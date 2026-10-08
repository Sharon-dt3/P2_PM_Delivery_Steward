"""Approving a risk-log proposal writes it to the risk log (PM-13 gate, PM-16/19 proposals, PM-26 batches, PM-28 surfaces).

Until now the risk proposals could only be rejected. Approving one now writes real rows to the CSV system of record, through the same
gate as every send: only an approver, only a proposal nobody has decided, every attempt logged, and the proposal marked applied.

What these tests pin down:

  what is written   the next RISK id, the tracker's own title, the proposal's own words, the evidence's date, status open; a batch item
                    carries the channel message that justifies it; the agent's silence about severity is not filled in by it
  severity          the approver's choice, or `medium` with the audit saying nobody chose; anything else is refused
  the gate          a stranger writes nothing; an edited approval, a tracker batch and a blocker already in the log are refused BEFORE
                    approval, so nothing is left half-approved; a write that fails can be retried and lands once
  both copies       the CSV and the runtime copy the detector reads: the same blocker is not proposed again tomorrow
  every surface     a card pressed in Teams, the command line and the dashboard leave the same rows
"""

from __future__ import annotations

import json
import shutil
import sqlite3

import pytest
from fastapi.testclient import TestClient
from spine.approval.proposals import APPLIED, APPROVED, PENDING, REJECTED, ProposalStore
from streamlit.testing.v1 import AppTest

from pm.adapters.risk_log import RiskLogMock
from pm.adapters.tracker import TrackerMock
from pm.api import copilot_studio_api as api
from pm.approval import cards, risk_apply, service
from pm.approval.audit import RISK_WRITE_TYPES as AUDIT_RISK_WRITE_TYPES
from pm.approval.audit import audit_trail, describe
from pm.channel.batches import TrackerView, consume
from pm.risk.gaps import find_gaps
from pm.risk.proposals import detect_and_propose
from pm.risk.scripted import ScriptedRiskGateway
from pm.risklog.csv_store import CsvRiskLog, live_risk_log_path
from pm.seed.build import ANCHOR_DATE
from pm.state.snapshot import build_current_snapshot

AS_OF = f"{ANCHOR_DATE.isoformat()}T12:00:00+00:00"
APPROVERS = service.ApprovalPolicy(approver_ids=frozenset({"sharon.silva"}))
CHANNEL = "19:proj-gamma@thread.tacv2"
RECORD = {
    "schema_version": "1.0", "channel_id": CHANNEL, "channel_display_name": "Project Gamma", "date": "2026-09-18", "allowlisted": True,
    "roster": [], "generated_at": "2026-09-18T11:30:00+00:00", "participation": [], "decisions": [], "questions": [], "updates": [],
    "blockers": [
        {"message_id": "m4", "text": "PM-014 is still blocked on the staging migration.", "quote": None},
        {"message_id": "m6", "text": "The adapter payload shape is undocumented.", "quote": None},
    ],
}


def risk_rows() -> list:
    return CsvRiskLog(live_risk_log_path()).list_risks()


@pytest.fixture()
def gaps(seeded_db_path) -> dict[str, str]:
    """item id -> the pending risk-log proposal for it (PM-014 and PM-015 have no entry in the log)."""
    snapshot = build_current_snapshot(seeded_db_path, taken_at=AS_OF, tz_name="Asia/Colombo")
    results = detect_and_propose(snapshot, ScriptedRiskGateway(find_gaps(snapshot)), db_path=seeded_db_path)
    return {r.item_id: r.proposal_id for r in results}


@pytest.fixture()
def batch(seeded_db_path, tmp_path) -> str:
    path = tmp_path / "2026-09-18.json"
    path.write_text(json.dumps(RECORD), encoding="utf-8")
    view = TrackerView.from_adapters(TrackerMock(db_path=seeded_db_path), RiskLogMock(db_path=seeded_db_path))
    return consume(path, view, db_path=seeded_db_path).risk.proposal_id


def approve(db, proposal_id, *, user="sharon.silva", **kw):
    return service.approve_and_send(proposal_id, approver_id=user, policy=APPROVERS, db_path=db, **kw)


def audit_actions(db, proposal_id) -> list[str]:
    return [e["action"] for e in audit_trail(proposal_id, db_path=db).events]


# --- what is written -------------------------------------------------------------------------------------------------------------


def test_approving_a_gap_writes_one_open_entry_built_from_the_proposal_and_the_tracker(seeded_db_path, gaps):
    before = {r.id for r in risk_rows()}
    payload = ProposalStore(seeded_db_path).get(gaps["PM-014"]).payload

    result = approve(seeded_db_path, gaps["PM-014"])

    (entry,) = [r for r in risk_rows() if r.id not in before]
    assert result.outcome == service.APPLIED_OUTCOME and entry.id == "RISK-004" and f"{entry.id}" in result.detail
    assert entry.related_item_id == "PM-014" and entry.status == "open"
    assert entry.title == f"{TrackerMock(db_path=seeded_db_path).get_item('PM-014').title} is blocked"
    assert payload["description"] in entry.description and payload["impact"] in entry.description
    assert entry.opened_at == payload["local_date"]  # the day the evidence is as of, not the day someone pressed Approve


def test_the_next_approval_takes_the_next_id(seeded_db_path, gaps):
    approve(seeded_db_path, gaps["PM-014"])
    approve(seeded_db_path, gaps["PM-015"])

    assert [r.id for r in risk_rows()][-2:] == ["RISK-004", "RISK-005"]


def test_a_batch_writes_every_item_with_the_message_that_justifies_it(seeded_db_path, batch):
    result = approve(seeded_db_path, batch, severity="high")

    written = [r for r in risk_rows() if r.id in ("RISK-004", "RISK-005")]
    assert result.outcome == service.APPLIED_OUTCOME and len(written) == 2
    by_item = {r.related_item_id: r for r in written}
    assert "From Project Gamma message m4 on 2026-09-18." in by_item["PM-014"].description
    assert "From Project Gamma message m6 on 2026-09-18." in by_item[None].description
    assert {r.severity for r in written} == {"high"} and {r.opened_at for r in written} == {"2026-09-18"}


def test_a_blocker_that_already_has_an_open_risk_is_skipped_in_a_batch_and_the_audit_says_so(seeded_db_path, batch):
    CsvRiskLog(live_risk_log_path()).create_risk(
        risk_apply.Risk(id="RISK-004", title="Staging migration", description="d", severity="high", status="open", related_item_id="PM-014",
                        opened_at="2026-09-17"))

    result = approve(seeded_db_path, batch)

    applied = next(e for e in audit_trail(batch, db_path=seeded_db_path).events if e["action"] == "proposal.applied")
    assert result.outcome == service.APPLIED_OUTCOME and "1 already covered" in result.detail
    assert [r.id for r in risk_rows()][-2:] == ["RISK-004", "RISK-005"] and applied["details"]["risk_ids"] == ["RISK-005"]
    assert applied["details"]["skipped"][0]["reason"] == "PM-014 already has an open risk"


# --- severity: the agent rates nothing, the approver does ------------------------------------------------------------------------


def test_the_approvers_severity_is_written_and_recorded_as_theirs(seeded_db_path, gaps):
    approve(seeded_db_path, gaps["PM-014"], severity="High")

    (entry,) = [r for r in risk_rows() if r.related_item_id == "PM-014"]
    approved = next(e for e in audit_trail(gaps["PM-014"], db_path=seeded_db_path).events if e["action"] == "proposal.approved")
    assert entry.severity == "high" and approved["details"] == {"edited": False, "severity": "high", "severity_source": "approver"}


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_no_severity_means_medium_and_the_audit_says_nobody_chose(seeded_db_path, gaps, blank):
    approve(seeded_db_path, gaps["PM-014"], severity=blank)

    (entry,) = [r for r in risk_rows() if r.related_item_id == "PM-014"]
    approved = next(e for e in audit_trail(gaps["PM-014"], db_path=seeded_db_path).events if e["action"] == "proposal.approved")
    assert entry.severity == "medium" and approved["details"]["severity_source"] == "default"
    assert "the default: nobody rated it" in describe(audit_trail(gaps["PM-014"], db_path=seeded_db_path))


def test_a_severity_the_log_does_not_have_is_refused_and_nothing_is_decided(seeded_db_path, gaps):
    # Two layers say no with the same words: choose_severity, and the risk log's own validation behind it. Removing either alone leaves
    # this behaviour intact (a known equivalent mutant), which is the point of having both.
    before = risk_rows()

    result = approve(seeded_db_path, gaps["PM-014"], severity="catastrophic")

    assert result.outcome == service.REFUSED and "low, medium, high" in result.detail
    assert ProposalStore(seeded_db_path).get(gaps["PM-014"]).status == PENDING and risk_rows() == before
    assert audit_actions(seeded_db_path, gaps["PM-014"])[-1] == "proposal.denied"


# --- the gate ---------------------------------------------------------------------------------------------------------------------


def test_a_stranger_cannot_write_to_the_risk_log(seeded_db_path, gaps):
    before = risk_rows()

    result = approve(seeded_db_path, gaps["PM-014"], user="mallory")

    assert result.outcome == service.REFUSED and risk_rows() == before
    assert ProposalStore(seeded_db_path).get(gaps["PM-014"]).status == PENDING
    assert audit_actions(seeded_db_path, gaps["PM-014"])[-1] == "proposal.denied"


def test_the_auto_approver_cannot_write_to_the_risk_log_either(seeded_db_path, gaps):
    result = service.auto_approve_and_send(gaps["PM-014"], policy=service.ApprovalPolicy(approver_ids=APPROVERS.approver_ids, auto_approve=True),
                                           db_path=seeded_db_path)

    assert result.outcome == service.HELD and "never auto-approved" in result.detail and len(risk_rows()) == 3


def test_a_risk_entry_cannot_be_edited_it_is_approved_as_proposed_or_not_at_all(seeded_db_path, gaps):
    before = risk_rows()

    result = approve(seeded_db_path, gaps["PM-014"], edited_content="something else entirely")

    assert result.outcome == service.REFUSED and "cannot be edited" in result.detail and risk_rows() == before
    assert ProposalStore(seeded_db_path).get(gaps["PM-014"]).status == PENDING


@pytest.mark.parametrize("nothing", ["", "   ", "\n"])
def test_a_flow_that_sends_a_blank_edit_for_a_card_with_no_edit_box_is_not_refused(seeded_db_path, gaps, nothing):
    result = approve(seeded_db_path, gaps["PM-014"], edited_content=nothing)

    assert result.outcome == service.APPLIED_OUTCOME


def test_a_blocker_that_got_an_open_risk_after_it_was_proposed_is_refused_not_written_twice(seeded_db_path, gaps):
    CsvRiskLog(live_risk_log_path()).create_risk(
        risk_apply.Risk(id="RISK-004", title="Added by the lead", description="d", severity="low", status="open", related_item_id="PM-014",
                        opened_at="2026-09-17"))
    before = risk_rows()

    result = approve(seeded_db_path, gaps["PM-014"])

    assert result.outcome == service.REFUSED and "already in the risk log as RISK-004" in result.detail
    assert risk_rows() == before and ProposalStore(seeded_db_path).get(gaps["PM-014"]).status == PENDING


def test_a_batch_where_everything_is_already_covered_is_refused(seeded_db_path, tmp_path):
    only_covered = dict(RECORD, blockers=[RECORD["blockers"][0]])
    path = tmp_path / "covered.json"
    path.write_text(json.dumps(only_covered), encoding="utf-8")
    view = TrackerView.from_adapters(TrackerMock(db_path=seeded_db_path), RiskLogMock(db_path=seeded_db_path))
    pid = consume(path, view, db_path=seeded_db_path).risk.proposal_id
    CsvRiskLog(live_risk_log_path()).create_risk(
        risk_apply.Risk(id="RISK-004", title="Staging", description="d", severity="low", status="open", related_item_id="PM-014", opened_at="2026-09-17"))

    result = approve(seeded_db_path, pid)

    assert result.outcome == service.REFUSED and "nothing to write" in result.detail and ProposalStore(seeded_db_path).get(pid).status == PENDING


def test_a_tracker_batch_still_cannot_be_approved(seeded_db_path, tmp_path):
    path = tmp_path / "r.json"
    path.write_text(json.dumps(RECORD), encoding="utf-8")
    view = TrackerView.from_adapters(TrackerMock(db_path=seeded_db_path), RiskLogMock(db_path=seeded_db_path))
    tracker = consume(path, view, db_path=seeded_db_path).tracker.proposal_id

    result = approve(seeded_db_path, tracker)

    assert result.outcome == service.REFUSED and "tracker change" in result.detail and ProposalStore(seeded_db_path).get(tracker).status == PENDING


def test_rejecting_a_risk_proposal_is_unchanged_and_writes_nothing(seeded_db_path, gaps):
    before = risk_rows()

    result = service.reject(gaps["PM-014"], approver_id="sharon.silva", reason="tracked elsewhere", policy=APPROVERS, db_path=seeded_db_path)

    assert result.outcome == service.REJECTED_OUTCOME and risk_rows() == before
    assert ProposalStore(seeded_db_path).get(gaps["PM-014"]).status == REJECTED


# --- the record it leaves --------------------------------------------------------------------------------------------------------


def test_the_write_is_audited_and_logged_like_any_other_send(seeded_db_path, gaps):
    approve(seeded_db_path, gaps["PM-014"], severity="medium")

    trail = audit_trail(gaps["PM-014"], db_path=seeded_db_path)
    assert audit_actions(seeded_db_path, gaps["PM-014"]) == ["proposal.created", "proposal.approved", "proposal.applied"]
    applied = trail.events[-1]
    assert applied["actor"] == "sharon.silva" and applied["details"]["risk_ids"] == ["RISK-004"] and applied["details"]["runtime_copy"] == "refreshed"
    assert [(a["action_type"], a["target"], a["status"]) for a in trail.send_attempts] == [("risk_log_write", "RISK-004", "sent")]
    assert trail.status == APPLIED and trail.approver_id == "sharon.silva" and trail.edited is False
    text = describe(trail)
    assert "Approved by sharon.silva" in text and "as proposed" in text and "Written to the risk log as RISK-004" in text


def test_the_decision_card_says_a_risk_entry_was_written_not_that_a_brief_was_sent(seeded_db_path, gaps):
    approve(seeded_db_path, gaps["PM-014"])

    card = cards.decision_card(audit_trail(gaps["PM-014"], db_path=seeded_db_path))

    text = json.dumps(card)
    assert "Risk-log proposal approved" in text and "Written to the risk log as" in text and "RISK-004" in text and "Morning brief" not in text


def test_the_audit_module_and_the_applier_agree_on_which_types_write_to_the_risk_log():
    assert AUDIT_RISK_WRITE_TYPES == risk_apply.RISK_WRITE_TYPES == service.RISK_WRITE_TYPES


# --- both copies of the log, so it is not proposed again -------------------------------------------------------------------------


def test_after_approval_the_runtime_copy_has_it_and_the_same_blocker_is_not_proposed_again(seeded_db_path, gaps):
    approve(seeded_db_path, gaps["PM-014"])

    assert "RISK-004" in {r.id for r in RiskLogMock(seeded_db_path).list_risks()}
    snapshot = build_current_snapshot(seeded_db_path, taken_at=AS_OF, tz_name="Asia/Colombo")
    assert "PM-014" not in {g.item_id for g in find_gaps(snapshot)}  # the detector no longer sees a gap
    assert "PM-015" in {g.item_id for g in find_gaps(snapshot)}  # and still sees the one that was not approved


def test_a_runtime_copy_that_cannot_be_refreshed_does_not_undo_the_write_and_the_audit_says_so(seeded_db_path, gaps, monkeypatch):
    monkeypatch.setattr(risk_apply, "refresh_runtime_copy", lambda *a, **k: "stale: OperationalError: database is locked")

    result = approve(seeded_db_path, gaps["PM-014"])

    applied = next(e for e in audit_trail(gaps["PM-014"], db_path=seeded_db_path).events if e["action"] == "proposal.applied")
    assert result.outcome == service.APPLIED_OUTCOME and "RISK-004" in {r.id for r in risk_rows()}
    assert applied["details"]["runtime_copy"].startswith("stale")


# --- a write that fails can be retried, and lands once ---------------------------------------------------------------------------


class FlakyLog(CsvRiskLog):
    def __init__(self, path, failures: int) -> None:
        super().__init__(path)
        self.failures = failures

    def create_risk(self, payload):
        if self.failures:
            self.failures -= 1
            raise OSError("disk full")
        return super().create_risk(payload)


def test_a_failed_write_leaves_the_proposal_approved_and_a_retry_writes_it_once(seeded_db_path, gaps):
    flaky = FlakyLog(live_risk_log_path(), failures=1)

    first = approve(seeded_db_path, gaps["PM-014"], risk_log=flaky, severity="high")

    assert first.outcome == service.SEND_FAILED and "disk full" in first.detail
    assert ProposalStore(seeded_db_path).get(gaps["PM-014"]).status == APPROVED and len(risk_rows()) == 3
    assert audit_actions(seeded_db_path, gaps["PM-014"])[-1] == "proposal.send_failed"

    retry = service.send_approved(gaps["PM-014"], policy=APPROVERS, risk_log=flaky, db_path=seeded_db_path)
    again = service.send_approved(gaps["PM-014"], policy=APPROVERS, risk_log=flaky, db_path=seeded_db_path)

    assert retry.outcome == service.APPLIED_OUTCOME and again.outcome == service.REFUSED  # applied once, never twice
    (entry,) = [r for r in risk_rows() if r.related_item_id == "PM-014"]
    assert entry.severity == "high"  # the retry keeps what the approver chose


# --- every surface leaves the same rows ------------------------------------------------------------------------------------------


def _record(db, proposal_id) -> dict:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        audit = [dict(r) for r in conn.execute("SELECT actor, action, details FROM audit WHERE entity_id = ? ORDER BY id", (proposal_id,))]
        writes = [dict(r) for r in conn.execute("SELECT action_type, target, status FROM write_log WHERE proposal_id = ? ORDER BY id", (proposal_id,))]
        proposal = dict(conn.execute("SELECT type, status, approver_id, payload FROM proposals WHERE id = ?", (proposal_id,)).fetchone())
    finally:
        conn.close()
    return {"audit": audit, "write_log": writes, "proposal": proposal}


def test_a_card_pressed_in_teams_writes_the_same_rows_as_the_command_line(seeded_db_path, gaps, tmp_path, monkeypatch):
    """Two copies of one pending proposal and of the risk log. One is approved by calling the service (what the command line and the
    dashboard do), the other by a card submitted over HTTP with the severity the approver picked."""
    other_db, other_csv = tmp_path / "fallback.db", tmp_path / "fallback_risks.csv"
    shutil.copy(seeded_db_path, other_db)
    shutil.copy(live_risk_log_path(), other_csv)
    service.approve_and_send(gaps["PM-014"], approver_id="sharon.silva", severity="high", policy=APPROVERS, risk_log=CsvRiskLog(other_csv), db_path=other_db)
    monkeypatch.setenv("PM_DB_PATH", str(seeded_db_path))
    monkeypatch.setenv("PM_COPILOT_API_KEY", "k")
    monkeypatch.setenv("PM_APPROVER_IDS", "sharon.silva")

    with TestClient(api.app) as client:
        result = client.post("/card_action", json={"action": "approve", "proposal_id": gaps["PM-014"], "severity": "high"},
                             headers={"X-API-Key": "k", "X-Authenticated-User": "sharon.silva"}).json()

    assert result["outcome"] == service.APPLIED_OUTCOME
    assert _record(seeded_db_path, gaps["PM-014"]) == _record(other_db, gaps["PM-014"])
    assert CsvRiskLog(live_risk_log_path()).list_risks() == CsvRiskLog(other_csv).list_risks()


def test_a_card_with_no_severity_picked_gets_the_recorded_default(seeded_db_path, gaps, monkeypatch):
    monkeypatch.setenv("PM_DB_PATH", str(seeded_db_path))
    monkeypatch.setenv("PM_COPILOT_API_KEY", "k")
    monkeypatch.setenv("PM_APPROVER_IDS", "sharon.silva")

    with TestClient(api.app) as client:  # a flow that has not been taught the severity field sends none
        result = client.post("/card_action", json={"action": "approve", "proposal_id": gaps["PM-014"]},
                             headers={"X-API-Key": "k", "X-Authenticated-User": "sharon.silva"}).json()

    approved = next(e for e in audit_trail(gaps["PM-014"], db_path=seeded_db_path).events if e["action"] == "proposal.approved")
    assert result["outcome"] == service.APPLIED_OUTCOME and approved["details"]["severity_source"] == "default"


def test_a_stranger_pressing_the_card_writes_nothing(seeded_db_path, gaps, monkeypatch):
    monkeypatch.setenv("PM_DB_PATH", str(seeded_db_path))
    monkeypatch.setenv("PM_COPILOT_API_KEY", "k")
    monkeypatch.setenv("PM_APPROVER_IDS", "sharon.silva")
    before = risk_rows()

    with TestClient(api.app) as client:
        result = client.post("/card_action", json={"action": "approve", "proposal_id": gaps["PM-014"], "severity": "low"},
                             headers={"X-API-Key": "k", "X-Authenticated-User": "mallory@example.com"}).json()

    assert result["outcome"] == service.REFUSED and risk_rows() == before


def test_the_command_line_takes_a_severity(seeded_db_path, gaps, monkeypatch, capsys):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("approve_cli", Path(__file__).resolve().parents[2] / "scripts" / "approve.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.setenv("PM_APPROVER_IDS", "sharon.silva")

    code = cli.main(["--db", str(seeded_db_path), "approve", gaps["PM-014"], "--as", "sharon.silva", "--severity", "low"])

    assert code == 0 and "applied: wrote RISK-004 to the risk log" in capsys.readouterr().out
    assert [r.severity for r in risk_rows() if r.related_item_id == "PM-014"] == ["low"]


def test_the_dashboard_approves_a_risk_entry_with_the_severity_picked(seeded_db_path, gaps, monkeypatch, tmp_path):
    from pathlib import Path

    monkeypatch.setenv("PM_DB_PATH", str(seeded_db_path))
    monkeypatch.setenv("P1_DB_PATH", str(tmp_path / "no_p1.db"))
    monkeypatch.setenv("PM_APPROVER_IDS", "sharon.silva")
    monkeypatch.setenv("TEAMS_PUBLISHER_MODE", "mock")
    monkeypatch.setenv("TEAMS_PUBLISHER_LOG_PATH", str(tmp_path / "log.jsonl"))
    monkeypatch.setenv("PM_AUTO_APPROVE", "0")
    monkeypatch.setenv("PM_DASHBOARD_USER", "")
    page = str(Path(__file__).resolve().parents[2] / "app" / "approval_dashboard.py")
    at = AppTest.from_file(page, default_timeout=30).run()
    at.selectbox(key="acting_as").select("sharon.silva").run()
    at.selectbox(key=f"severity_{gaps['PM-014']}").select("high").run()

    next(b for b in at.button if b.key == f"approve_{gaps['PM-014']}").click().run()

    assert not at.exception, [e.value for e in at.exception]
    assert [r.severity for r in risk_rows() if r.related_item_id == "PM-014"] == ["high"]
    assert ProposalStore(seeded_db_path).get(gaps["PM-014"]).status == APPLIED
