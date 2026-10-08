"""PM-16: risk proposals ride the PM-13 approval gate, but they are not Teams posts.

The gate's executor sends a message to a channel or a person. A risk proposal posts nothing: approving
it WRITES the entry to the risk log (pm.approval.risk_apply; tests/unit/test_risk_approval.py covers that in
depth). What stays true, and is tested here: only an approver can do it, an edited approval is refused, the
type is never posted anywhere, auto-approve never touches it, and a person can still reject it, on the
record. The dashboard shows it readably and offers Approve (with a severity) and Reject.

Also: the morning job runs detection when PM_RISK_DETECTION=1, and scripts/detect_risks.py
runs it on demand.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from datetime import datetime, time, timezone
from pathlib import Path

import pytest
from p1.adapters.teams_publisher_mock import LogPublisher
from spine.approval.proposals import APPROVED, PENDING, REJECTED, ProposalStore
from streamlit.testing.v1 import AppTest

from pm.approval import service
from pm.approval.audit import audit_trail
from pm.approval.service import ApprovalPolicy
from pm.risk.gaps import find_gaps
from pm.risk.proposals import RISK_PROPOSAL_TYPE, detect_and_propose
from pm.risk.scripted import ScriptedRiskGateway
from pm.seed.build import ANCHOR_DATE
from pm.state.snapshot import build_current_snapshot

AS_OF = f"{ANCHOR_DATE.isoformat()}T12:00:00+00:00"
POLICY = ApprovalPolicy(approver_ids=frozenset({"sharon.silva"}), auto_approve=True, auto_approve_requires_first_human=False)


@pytest.fixture()
def log(tmp_path):
    return LogPublisher(tmp_path / "log.jsonl")


@pytest.fixture()
def pending_id(seeded_db_path):
    snapshot = build_current_snapshot(seeded_db_path, taken_at=AS_OF, tz_name="Asia/Colombo")
    results = detect_and_propose(snapshot, ScriptedRiskGateway(find_gaps(snapshot)), db_path=seeded_db_path)
    return results[0].proposal_id


def _write_log_rows(db):
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT count(*) FROM write_log").fetchone()[0]
    finally:
        conn.close()


# --- approving writes to the risk log and posts nothing; the rest of the gate still holds ------------------------------


def test_approving_a_risk_proposal_writes_it_to_the_risk_log_and_posts_nothing(seeded_db_path, pending_id, log):
    from pm.risklog.csv_store import CsvRiskLog, live_risk_log_path

    before = {r.id for r in CsvRiskLog(live_risk_log_path()).list_risks()}
    outcome = service.approve_and_send(pending_id, approver_id="sharon.silva", publisher=log, policy=POLICY, db_path=seeded_db_path)

    assert outcome.outcome == "applied" and ProposalStore(seeded_db_path).get(pending_id).status == "applied"
    written = {r.id for r in CsvRiskLog(live_risk_log_path()).list_risks()} - before
    assert len(written) == 1 and log.read_log() == []  # one entry in the risk log; nothing posted to Teams
    assert _write_log_rows(seeded_db_path) == 1  # and the write is on the same record as every send


def test_an_edited_approval_is_refused_too(seeded_db_path, pending_id, log):
    outcome = service.approve_and_send(pending_id, approver_id="sharon.silva", edited_content="something else",
                                       publisher=log, policy=POLICY, db_path=seeded_db_path)

    assert outcome.outcome == "refused" and ProposalStore(seeded_db_path).get(pending_id).status == PENDING


def test_auto_approve_never_takes_a_risk_proposal(seeded_db_path, pending_id, log):
    outcome = service.auto_approve_and_send(pending_id, publisher=log, policy=POLICY, db_path=seeded_db_path)

    assert outcome.outcome in ("held", "refused") and ProposalStore(seeded_db_path).get(pending_id).status == PENDING
    assert log.read_log() == []


def test_the_explanation_says_why_it_is_waiting(seeded_db_path, pending_id, log):
    why = service.explain_hold(pending_id, policy=POLICY, publisher=log, db_path=seeded_db_path)

    assert why and "risk" in why.lower() and "person" in why.lower()


def test_sending_and_the_internal_executor_refuse_a_proposal_nobody_approved(seeded_db_path, pending_id, log):
    from pm.risklog.csv_store import CsvRiskLog, live_risk_log_path

    store = ProposalStore(seeded_db_path)
    before = CsvRiskLog(live_risk_log_path()).list_risks()

    assert service.send_approved(pending_id, publisher=log, policy=POLICY, db_path=seeded_db_path).outcome == "refused"
    assert service._execute(pending_id, actor="agent", publisher=log, policy=POLICY, store=store, db_path=seeded_db_path).outcome == "refused"
    assert log.read_log() == [] and CsvRiskLog(live_risk_log_path()).list_risks() == before  # not approved: not written


def test_a_risk_proposal_is_never_posted_anywhere_even_when_approved_around_the_service(seeded_db_path, pending_id, log):
    """Defence in depth: the executor writes an approved risk proposal to the risk log (once) and posts nothing."""
    store = ProposalStore(seeded_db_path)
    store.approve(pending_id, approver_id="sharon.silva")  # bypassing the service on purpose

    outcome = service.send_approved(pending_id, publisher=log, policy=POLICY, db_path=seeded_db_path)
    again = service.send_approved(pending_id, publisher=log, policy=POLICY, db_path=seeded_db_path)

    assert outcome.outcome == "applied" and log.read_log() == [] and store.get(pending_id).status == "applied"
    assert again.outcome == "refused"  # already applied: it is not written twice


# --- a person can reject one, on the record ----------------------------------------------------------------------------


def test_a_person_can_reject_it_and_it_is_audited(seeded_db_path, pending_id):
    outcome = service.reject(pending_id, approver_id="sharon.silva", reason="PM-014 is already tracked elsewhere",
                             policy=POLICY, db_path=seeded_db_path)

    trail = audit_trail(pending_id, db_path=seeded_db_path)
    assert outcome.outcome == "rejected" and trail.status == REJECTED
    assert trail.approver_id == "sharon.silva" and trail.decided_at
    assert trail.original_proposal["description"] == trail.final_proposal["description"]  # what the agent proposed is on record
    assert [e["action"] for e in trail.events] == ["proposal.created", "proposal.rejected"]


def test_a_stranger_cannot_reject_it(seeded_db_path, pending_id):
    outcome = service.reject(pending_id, approver_id="mallory", policy=POLICY, db_path=seeded_db_path)

    assert outcome.outcome == "refused" and ProposalStore(seeded_db_path).get(pending_id).status == PENDING


def test_the_brief_still_works_exactly_as_before(seeded_db_path, log):
    """The new rule is about the proposal's TYPE: morning briefs are unaffected."""
    from pm.eval.pm12_cases import ScriptedGateway
    from pm.jobs.morning_brief_job import run_morning_brief_job
    from pm.scheduling.config import ProjectScheduleConfig
    from pm.seed.build import CHANNEL_ID

    config = ProjectScheduleConfig(channel_id=CHANNEL_ID, timezone="Asia/Colombo", working_days=["Mon", "Tue", "Wed", "Thu", "Fri"],
                                   morning_brief_time=time(8, 0), end_of_day_time=time(17, 0))
    result = run_morning_brief_job(config, ScriptedGateway(), moment=datetime(2026, 9, 16, 2, 30, tzinfo=timezone.utc),
                                   db_path=seeded_db_path, policy=ApprovalPolicy())

    outcome = service.approve_and_send(result.proposal_id, approver_id="sharon.silva", publisher=log, policy=POLICY, db_path=seeded_db_path)

    assert outcome.outcome == "sent" and len(log.read_log()) == 1


# --- the dashboard ----------------------------------------------------------------------------------------------------------


@pytest.fixture()
def dashboard(monkeypatch, seeded_db_path, tmp_path):
    monkeypatch.setenv("PM_DB_PATH", str(seeded_db_path))
    monkeypatch.setenv("P1_DB_PATH", str(tmp_path / "no_p1.db"))
    monkeypatch.setenv("PM_APPROVER_IDS", "sharon.silva")
    monkeypatch.setenv("TEAMS_PUBLISHER_MODE", "mock")
    monkeypatch.setenv("TEAMS_PUBLISHER_LOG_PATH", str(tmp_path / "log.jsonl"))
    monkeypatch.setenv("PM_AUTO_APPROVE", "0")
    monkeypatch.setenv("PM_DASHBOARD_USER", "")
    return str(Path(__file__).resolve().parents[2] / "app" / "approval_dashboard.py")


def _page_text(at):
    parts = []
    for kind in ("markdown", "caption", "text", "info", "warning", "subheader", "header"):
        parts += [str(el.value) for el in getattr(at, kind)]
    return "\n".join(parts)


def test_the_dashboard_shows_a_risk_proposal_readably_and_offers_approve_with_a_severity_and_reject(dashboard, pending_id):
    at = AppTest.from_file(dashboard, default_timeout=30).run()

    assert not at.exception, [e.value for e in at.exception]
    text = _page_text(at)
    assert "PM-014" in text and "Olivia Dupree" in text and "assignee of PM-014" in text
    keys = {b.key for b in at.button}
    assert f"reject_{pending_id}" in keys and f"approve_{pending_id}" in keys
    assert at.selectbox(key=f"severity_{pending_id}").value == "medium"  # the agent rates nothing: the approver does
    assert "writes this to the risk log" in text.lower()
    assert f"edit_{pending_id}" not in {t.key for t in at.text_area}  # no edit box on a risk entry


def test_rejecting_from_the_dashboard_works(dashboard, pending_id, seeded_db_path):
    at = AppTest.from_file(dashboard, default_timeout=30).run()
    at.selectbox(key="acting_as").select("sharon.silva").run()

    at.button(key=f"reject_{pending_id}").click().run()

    assert ProposalStore(seeded_db_path).get(pending_id).status == REJECTED


# --- the morning job --------------------------------------------------------------------------------------------------------


def _config():
    from pm.scheduling.config import ProjectScheduleConfig
    from pm.seed.build import CHANNEL_ID

    return ProjectScheduleConfig(channel_id=CHANNEL_ID, timezone="Asia/Colombo", working_days=["Mon", "Tue", "Wed", "Thu", "Fri"],
                                 morning_brief_time=time(8, 0), end_of_day_time=time(17, 0))


def _job(db, gateway):
    from pm.jobs.morning_brief_job import run_morning_brief_job

    return run_morning_brief_job(_config(), gateway, moment=datetime(2026, 9, 18, 2, 30, tzinfo=timezone.utc), db_path=db,
                                 policy=ApprovalPolicy())


class _Both:
    """One scripted model serving both the brief and the risk prose."""

    def __init__(self, db):
        from pm.eval.pm12_cases import ScriptedGateway

        snapshot = build_current_snapshot(db, taken_at="2026-09-18T02:30:00+00:00", tz_name="Asia/Colombo")
        self.brief, self.risk = ScriptedGateway(), ScriptedRiskGateway(find_gaps(snapshot))

    def generate(self, prompt, **kwargs):
        return (self.risk if "reference_id: item:" in prompt and "risk log entry" in prompt.lower() else self.brief).generate(prompt, **kwargs)


def _risk_count(db):
    store = ProposalStore(db)
    return sum(1 for s in ("pending", "approved", "rejected", "applied") for p in store.list_by_status(s) if p.type == RISK_PROPOSAL_TYPE)


def test_the_job_does_not_run_detection_unless_switched_on(seeded_db_path, monkeypatch):
    monkeypatch.setenv("PM_RISK_DETECTION", "0")

    result = _job(seeded_db_path, _Both(seeded_db_path))

    assert _risk_count(seeded_db_path) == 0 and result.risk_proposals == 0


def test_when_switched_on_the_job_proposes_the_missing_blockers_alongside_the_brief(seeded_db_path, monkeypatch):
    monkeypatch.setenv("PM_RISK_DETECTION", "1")

    result = _job(seeded_db_path, _Both(seeded_db_path))

    assert result.status == "generated" and result.proposal_id  # the brief is unaffected
    assert result.risk_proposals == 2 and _risk_count(seeded_db_path) == 2


def test_a_failure_in_detection_never_stops_the_brief(seeded_db_path, monkeypatch):
    monkeypatch.setenv("PM_RISK_DETECTION", "1")
    import pm.jobs.morning_brief_job as job

    monkeypatch.setattr(job, "detect_and_propose", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("model fell over")))

    result = _job(seeded_db_path, _Both(seeded_db_path))

    assert result.status == "generated" and result.proposal_id and result.risk_proposals == 0


def test_a_second_morning_does_not_repeat_the_proposals(seeded_db_path, monkeypatch):
    monkeypatch.setenv("PM_RISK_DETECTION", "1")
    _job(seeded_db_path, _Both(seeded_db_path))

    _job(seeded_db_path, _Both(seeded_db_path))

    assert _risk_count(seeded_db_path) == 2


# --- the command line --------------------------------------------------------------------------------------------------------


@pytest.fixture()
def cli():
    path = Path(__file__).resolve().parents[2] / "scripts" / "detect_risks.py"
    spec = importlib.util.spec_from_file_location("detect_risks_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_dry_run_lists_the_gaps_and_creates_nothing(cli, seeded_db_path, capsys):
    code = cli.main(["--db", str(seeded_db_path), "--at", "2026-09-18T12:00", "--dry-run"])

    out = capsys.readouterr().out
    assert code == 0 and "PM-014" in out and "PM-015" in out and "PM-023" not in out and "dry run" in out.lower()
    assert _risk_count(seeded_db_path) == 0


def test_the_command_proposes_the_two_missing_blockers(cli, seeded_db_path, capsys):
    code = cli.main(["--db", str(seeded_db_path), "--at", "2026-09-18T12:00", "--gateway", "scripted"])

    out = capsys.readouterr().out
    assert code == 0 and _risk_count(seeded_db_path) == 2
    assert "PM-014" in out and "PM-015" in out and "approve.py list" in out


def test_running_the_command_twice_proposes_nothing_new(cli, seeded_db_path, capsys):
    cli.main(["--db", str(seeded_db_path), "--at", "2026-09-18T12:00", "--gateway", "scripted"])
    capsys.readouterr()

    cli.main(["--db", str(seeded_db_path), "--at", "2026-09-18T12:00", "--gateway", "scripted"])

    assert _risk_count(seeded_db_path) == 2 and "already proposed" in capsys.readouterr().out.lower()
