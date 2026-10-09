"""Approving the weekly status report: it is SAVED as the final version, never sent.

The plan says the agent never sends the weekly report; a person does. So approving it means "this is the version I will send": the text is written,
exactly as proposed, to one file per week ending, through the same write gate as every other write, and the audit says where and says it was not sent.
It cannot be edited (a changed figure would no longer recompute), and auto-approve never takes it.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from p1.adapters.teams_publisher_mock import LogPublisher
from spine.approval.proposals import ProposalStore
from spine.approval.write_guard import WriteRefusedError, guarded_send

from pm.approval import cards, report_apply, service
from pm.approval.audit import audit_trail, describe
from pm.jobs import weekly_report_job as job

TZ = "Asia/Colombo"
FRIDAY = datetime(2026, 9, 18, 12, 30, tzinfo=timezone.utc)
POLICY = service.ApprovalPolicy(approver_ids=frozenset({"sharon"}))


@pytest.fixture()
def made(seeded_db_path):
    return job.run_weekly_report_job(FRIDAY, db_path=seeded_db_path, timezone_name=TZ)


@pytest.fixture()
def log(tmp_path):
    return LogPublisher(tmp_path / "out.jsonl")


def approve(made, db, log, who="sharon", **kw):
    return service.approve_and_send(made.proposal_id, approver_id=who, policy=POLICY, publisher=log, db_path=db, **kw)


def saved_file():
    return report_apply.reports_dir() / "weekly_report_2026-09-18.md"


def actions(pid, db):
    return [e["action"] for e in audit_trail(pid, db_path=db).events]


# --- the card -------------------------------------------------------------------------------------------------------------------------------


def test_the_card_offers_approve_and_reject_shows_the_whole_report_and_has_no_edit_box(seeded_db_path, made):
    card = next(a for a in cards.handle_list_pending({}, db_path=seeded_db_path)["approvals"] if a["proposal_id"] == made.proposal_id)["card"]

    assert [b["title"] for b in card["actions"]] == ["Approve", "Reject"]
    assert not [e for e in card["body"] if e.get("type", "").startswith("Input.")]  # no edit box, no severity picker
    assert any("Weekly status report, week ending 2026-09-18" in e.get("text", "") for e in card["body"])
    assert any("The agent never sends it: you send it yourself" in e.get("text", "") for e in card["body"])


def test_a_weekly_report_is_not_a_message_a_risk_write_or_a_tracker_write():
    assert service.REPORT_WRITE_TYPES <= service.EXECUTABLE_TYPES
    assert not service.REPORT_WRITE_TYPES & (service.MESSAGE_TYPES | service.RISK_WRITE_TYPES | service.TRACKER_WRITE_TYPES)


# --- approving ------------------------------------------------------------------------------------------------------------------------------


def test_approving_saves_the_report_exactly_as_proposed_and_sends_nothing(seeded_db_path, made, log):
    result = approve(made, seeded_db_path, log)

    assert result.outcome == service.APPLIED_OUTCOME and "not sent anywhere" in result.detail
    assert saved_file().read_text(encoding="utf-8") == made.text.rstrip("\n") + "\n"
    assert log.read_log() == []  # no post, to any channel, ever
    assert ProposalStore(seeded_db_path).get(made.proposal_id).status == "applied"


def test_the_audit_says_who_approved_when_where_it_was_saved_and_that_it_was_not_sent(seeded_db_path, made, log):
    approve(made, seeded_db_path, log)

    trail = audit_trail(made.proposal_id, db_path=seeded_db_path)
    applied = next(e for e in trail.events if e["action"] == "proposal.applied")["details"]

    assert [a for a in actions(made.proposal_id, seeded_db_path) if a.startswith("proposal.")][-2:] == ["proposal.approved", "proposal.applied"]
    assert trail.approver_id == "sharon" and not trail.edited
    assert applied == {"target": "report_file", "path": str(saved_file()), "sent": False, "already_saved": False}
    assert f"Saved as the final report at {trail.sent['created_at']}: {saved_file()}. It was not sent anywhere" in describe(trail)


def test_approving_from_a_teams_card_writes_the_same_audit_as_the_command_line(seeded_db_path, made):
    outcome = cards.handle_card_action({"action": "approve", "proposal_id": made.proposal_id}, authenticated_user_id="sharon", policy=POLICY, db_path=seeded_db_path)

    assert outcome["outcome"] == service.APPLIED_OUTCOME and saved_file().exists()
    assert audit_trail(made.proposal_id, db_path=seeded_db_path).approver_id == "sharon"
    card = cards.decision_card(audit_trail(made.proposal_id, db_path=seeded_db_path))
    facts = {f["title"]: f["value"] for e in card["body"] if e.get("type") == "FactSet" for f in e["facts"]}
    assert facts["Saved as the final report (not sent)"] == str(saved_file()) and "Weekly report approved" in card["body"][0]["text"]


def test_a_person_who_is_not_an_approver_cannot_approve_it(seeded_db_path, made, log):
    result = approve(made, seeded_db_path, log, who="stranger")

    assert result.outcome == service.REFUSED and not saved_file().exists()
    assert ProposalStore(seeded_db_path).get(made.proposal_id).status == "pending"


def test_it_cannot_be_edited_a_changed_report_is_refused_and_stays_pending(seeded_db_path, made, log):
    result = approve(made, seeded_db_path, log, edited_content=made.text.replace("up 50%", "up 500%"))

    assert result.outcome == service.REFUSED and "cannot be edited" in result.detail
    assert not saved_file().exists() and ProposalStore(seeded_db_path).get(made.proposal_id).status == "pending"
    assert "proposal.denied" in actions(made.proposal_id, seeded_db_path)


def test_sending_the_report_unchanged_in_the_edit_field_is_not_an_edit(seeded_db_path, made, log):
    assert approve(made, seeded_db_path, log, edited_content=made.text + "\r\n").outcome == service.APPLIED_OUTCOME


def test_auto_approve_never_takes_a_weekly_report(seeded_db_path, made, log):
    policy = service.ApprovalPolicy(approver_ids=frozenset({"sharon"}), auto_approve=True, auto_approve_requires_first_human=False)

    service.auto_approve_and_send(made.proposal_id, publisher=log, policy=policy, db_path=seeded_db_path)

    assert ProposalStore(seeded_db_path).get(made.proposal_id).status == "pending" and not saved_file().exists()


# --- rejecting, the write gate, and idempotence ---------------------------------------------------------------------------------------------------


def test_a_rejected_report_is_never_saved_and_cannot_be_saved_by_going_round_the_service(seeded_db_path, made):
    service.reject(made.proposal_id, approver_id="sharon", policy=POLICY, db_path=seeded_db_path)
    plan = report_apply.plan_save(ProposalStore(seeded_db_path).get(made.proposal_id))

    with pytest.raises(WriteRefusedError):
        guarded_send(made.proposal_id, action_type="report_save", target=str(plan.path), send_fn=lambda: report_apply.save(plan),
                     store=ProposalStore(seeded_db_path), db_path=seeded_db_path)

    assert not saved_file().exists()


def test_a_pending_report_cannot_be_saved_by_going_round_the_service_either(seeded_db_path, made):
    plan = report_apply.plan_save(ProposalStore(seeded_db_path).get(made.proposal_id))

    with pytest.raises(WriteRefusedError):
        guarded_send(made.proposal_id, action_type="report_save", target=str(plan.path), send_fn=lambda: report_apply.save(plan),
                     store=ProposalStore(seeded_db_path), db_path=seeded_db_path)

    assert not saved_file().exists()


def test_approving_twice_saves_once_and_the_second_is_refused(seeded_db_path, made, log):
    approve(made, seeded_db_path, log)
    before = saved_file().stat().st_mtime_ns

    again = approve(made, seeded_db_path, log)

    assert again.outcome == service.REFUSED and saved_file().stat().st_mtime_ns == before


def test_a_file_for_that_week_with_different_text_is_never_overwritten_and_the_approval_is_refused_before_it_is_recorded(seeded_db_path, made, log):
    saved_file().parent.mkdir(parents=True)
    saved_file().write_text("a report someone else saved\n", encoding="utf-8")

    result = approve(made, seeded_db_path, log)

    assert result.outcome == service.REFUSED and "never overwritten" in result.detail
    assert saved_file().read_text(encoding="utf-8") == "a report someone else saved\n"
    assert ProposalStore(seeded_db_path).get(made.proposal_id).status == "pending"  # not left approved-but-unsaved


def test_a_failed_save_leaves_the_report_approved_and_a_retry_saves_it_once(seeded_db_path, made, log, monkeypatch):
    report_apply.reports_dir().parent.mkdir(parents=True)
    report_apply.reports_dir().write_text("this is a file where the folder should be")  # the save cannot create its folder

    failed = approve(made, seeded_db_path, log)

    assert failed.outcome == service.SEND_FAILED and ProposalStore(seeded_db_path).get(made.proposal_id).status == "approved"
    assert "proposal.send_failed" in actions(made.proposal_id, seeded_db_path)
    report_apply.reports_dir().unlink()

    def no_publisher():
        raise RuntimeError("Teams is not configured")

    monkeypatch.setattr(service, "get_teams_publisher", no_publisher)  # saving a report needs no Teams publisher, so a retry must not need one

    retried = service.send_approved(made.proposal_id, policy=POLICY, db_path=seeded_db_path)

    assert retried.outcome == service.APPLIED_OUTCOME and saved_file().exists() and log.read_log() == []


def test_the_save_is_atomic_no_partial_file_is_left_behind(seeded_db_path, made, log):
    approve(made, seeded_db_path, log)

    assert [p.name for p in report_apply.reports_dir().iterdir()] == ["weekly_report_2026-09-18.md"]


def test_a_report_with_no_week_ending_or_no_text_cannot_be_saved(seeded_db_path):
    store = ProposalStore(seeded_db_path)
    bad_date = store.create(type="weekly_status_report", payload={"local_date": "not a date", "content": "x"}, original_model_output={}, source_refs=[], idempotency_key="a")
    empty = store.create(type="weekly_status_report", payload={"local_date": "2026-09-18", "content": "  \n"}, original_model_output={}, source_refs=[], idempotency_key="b")

    for proposal, why in ((bad_date, "no valid week ending"), (empty, "empty")):
        with pytest.raises(report_apply.ReportApplyRefused, match=why):
            report_apply.plan_save(proposal)


def test_retrying_a_report_that_was_never_approved_or_was_rejected_saves_nothing(seeded_db_path, made, log):
    pending = service.send_approved(made.proposal_id, policy=POLICY, publisher=log, db_path=seeded_db_path)
    service.reject(made.proposal_id, approver_id="sharon", policy=POLICY, db_path=seeded_db_path)
    rejected = service.send_approved(made.proposal_id, policy=POLICY, publisher=log, db_path=seeded_db_path)

    assert pending.outcome == service.REFUSED and rejected.outcome == service.REFUSED and not saved_file().exists()
