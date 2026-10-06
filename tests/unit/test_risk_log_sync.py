"""PM-15: the risk log in three places that must stay in sync.

  risk_log/risks.csv          committed, the repo's system of record (offline, reproducible)
  the lead-facing store       an editable table the delivery lead opens and edits (Dataverse's
                              role; Supabase stands in for it here) -- an in-memory fake below
  the risks table in pm.db    what the brief reads at run time, always loaded from the CSV

Syncing is three-way against the last agreed state, so it knows which side moved:
a lead's edit flows into the repo, a repo edit flows to the lead, and if BOTH moved it
reports a conflict and changes nothing. A bad edit or an unreachable store never
corrupts the repo copy.

The acceptance test: the same three seeded risks are visible in both stores and stay
in sync.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, time, timezone

import pytest

from pm.adapters.risk_log import Risk, RiskLogMock, RiskLogStore
from pm.risklog.csv_store import CsvRiskLog, read_risks, write_risks
from pm.risklog.remote import RemoteUnavailableError
from pm.risklog.sync import (
    CONFLICT,
    IN_SYNC,
    INVALID_LOCAL,
    INVALID_REMOTE,
    PULLED,
    PUSHED,
    REMOTE_UNREACHABLE,
    RiskLogSync,
)
from pm.seed.build import SEED_RISK_LOG_PATH

SEEDED = read_risks(SEED_RISK_LOG_PATH)


class InMemoryRemote(RiskLogStore):
    """The lead-facing store, as a fake: a table the lead can edit directly."""

    def __init__(self, risks=()):
        self.rows = {r.id: r for r in risks}
        self.writes = 0
        self.down = False

    def _up(self):
        if self.down:
            raise RemoteUnavailableError("could not reach the lead-facing store")

    def list_risks(self):
        self._up()
        return sorted(self.rows.values(), key=lambda r: r.id)

    def get_risk(self, risk_id):
        self._up()
        return self.rows[risk_id]

    def create_risk(self, payload):
        self._up()
        self.rows[payload.id] = payload
        self.writes += 1
        return payload

    def update_risk(self, risk_id, payload):
        self._up()
        self.rows[risk_id] = payload
        self.writes += 1
        return payload

    def replace_all(self, risks):
        self._up()
        self.rows = {r.id: r for r in risks}
        self.writes += 1

    # what the lead does in the table editor
    def lead_edits(self, risk_id, **changes):
        self.rows[risk_id] = self.rows[risk_id].model_copy(update=changes)

    def lead_adds(self, risk):
        self.rows[risk.id] = risk

    def lead_deletes(self, risk_id):
        del self.rows[risk_id]


@pytest.fixture()
def world(tmp_path, seeded_db_path):
    csv_path = tmp_path / "risks.csv"
    write_risks(csv_path, SEEDED)
    remote = InMemoryRemote()
    sync = RiskLogSync(CsvRiskLog(csv_path), remote, db_path=seeded_db_path, baseline_path=tmp_path / "baseline.json")
    return sync, remote, csv_path, seeded_db_path, tmp_path


def _new_risk(**changes):
    values = {"id": "RISK-004", "title": "Lead-added risk", "description": "Added in the table.", "severity": "high",
              "status": "open", "related_item_id": "PM-014", "opened_at": "2026-09-22"}
    return Risk(**{**values, **changes})


def _sqlite_risks(db):
    return RiskLogMock(db).list_risks()


# --- first sync, and staying in sync ---------------------------------------------------------------------------------


def test_the_first_sync_pushes_the_repo_log_to_the_empty_lead_store(world):
    sync, remote, csv_path, db, _ = world  # noqa: RUF059 - the fixture's other parts are not needed here

    outcome = sync.sync()

    assert outcome.action == PUSHED
    assert remote.list_risks() == SEEDED


def test_a_second_sync_changes_nothing(world):
    sync, remote, csv_path, db, _ = world  # noqa: RUF059 - the fixture's other parts are not needed here
    sync.sync()
    writes, csv_bytes = remote.writes, csv_path.read_bytes()

    outcome = sync.sync()

    assert outcome.action == IN_SYNC and remote.writes == writes and csv_path.read_bytes() == csv_bytes


def test_the_agreed_state_is_remembered_between_runs(world):
    sync, remote, csv_path, db, tmp_path = world  # noqa: RUF059 - the fixture's other parts are not needed here
    sync.sync()

    baseline = json.loads((tmp_path / "baseline.json").read_text())

    assert baseline["fingerprint"] and baseline["synced_at"]


# --- a lead's edit flows into the repo ----------------------------------------------------------------------------------


def test_a_lead_edit_in_the_table_is_pulled_into_the_repo_and_the_runtime_copy(world):
    sync, remote, csv_path, db, _ = world
    sync.sync()
    remote.lead_edits("RISK-001", status="mitigated", description="Fixed by the 14 Sep deploy.")
    remote.lead_adds(_new_risk())

    outcome = sync.sync()

    assert outcome.action == PULLED
    repo = {r.id: r for r in read_risks(csv_path)}
    assert repo["RISK-001"].status == "mitigated" and repo["RISK-001"].description == "Fixed by the 14 Sep deploy."
    assert "RISK-004" in repo
    assert _sqlite_risks(db) == read_risks(csv_path)  # the brief's own copy follows the repo


def test_a_row_the_lead_deletes_is_removed_from_the_repo_too(world):
    sync, remote, csv_path, db, _ = world
    sync.sync()
    remote.lead_deletes("RISK-003")

    assert sync.sync().action == PULLED

    assert [r.id for r in read_risks(csv_path)] == ["RISK-001", "RISK-002"]
    assert [r.id for r in _sqlite_risks(db)] == ["RISK-001", "RISK-002"]


# --- a repo edit flows to the lead ------------------------------------------------------------------------------------


def test_a_repo_edit_is_pushed_to_the_lead_store(world):
    sync, remote, csv_path, db, _ = world
    sync.sync()
    CsvRiskLog(csv_path).update_risk("RISK-002", SEEDED[1].model_copy(update={"severity": "medium"}))

    outcome = sync.sync()

    assert outcome.action == PUSHED
    assert remote.rows["RISK-002"].severity == "medium"
    assert _sqlite_risks(db) == read_risks(csv_path)


def test_a_new_repo_risk_and_a_deleted_one_both_reach_the_lead_store(world):
    sync, remote, csv_path, db, _ = world  # noqa: RUF059 - the fixture's other parts are not needed here
    sync.sync()
    log = CsvRiskLog(csv_path)
    log.create_risk(_new_risk(id="RISK-005"))
    log.replace_all([r for r in log.list_risks() if r.id != "RISK-001"])

    assert sync.sync().action == PUSHED

    assert sorted(remote.rows) == ["RISK-002", "RISK-003", "RISK-005"]


# --- both moved: a conflict, and nothing changes ---------------------------------------------------------------------------


def test_edits_on_both_sides_are_a_conflict_and_nothing_is_overwritten(world):
    sync, remote, csv_path, db, _ = world  # noqa: RUF059 - the fixture's other parts are not needed here
    sync.sync()
    CsvRiskLog(csv_path).update_risk("RISK-001", SEEDED[0].model_copy(update={"severity": "high"}))
    remote.lead_edits("RISK-002", status="closed")
    csv_before, remote_before = csv_path.read_bytes(), dict(remote.rows)

    outcome = sync.sync()

    assert outcome.action == CONFLICT
    assert "RISK-001" in outcome.detail and "RISK-002" in outcome.detail  # says what differs
    assert csv_path.read_bytes() == csv_before and remote.rows == remote_before


def test_a_conflict_can_be_resolved_by_choosing_a_side(world):
    sync, remote, csv_path, db, _ = world  # noqa: RUF059 - the fixture's other parts are not needed here
    sync.sync()
    CsvRiskLog(csv_path).update_risk("RISK-001", SEEDED[0].model_copy(update={"severity": "high"}))
    remote.lead_edits("RISK-002", status="closed")

    assert sync.sync().action == CONFLICT
    assert sync.pull().action == PULLED  # the lead's version wins

    assert read_risks(csv_path)[1].status == "closed" and read_risks(csv_path)[0].severity == "medium"
    assert sync.sync().action == IN_SYNC


def test_an_emptied_lead_store_cannot_wipe_the_repo_log(world):
    """If the lead's table is suddenly empty (dropped, mis-filtered, a bad import),
    that is not an instruction to delete the system of record."""
    sync, remote, csv_path, db, _ = world  # noqa: RUF059 - the fixture's other parts are not needed here
    sync.sync()
    remote.rows = {}

    outcome = sync.sync()

    assert outcome.action == CONFLICT and "empty" in outcome.detail.lower()
    assert [r.id for r in read_risks(csv_path)] == ["RISK-001", "RISK-002", "RISK-003"]
    assert sync.pull().action == PULLED  # a person can still choose to accept it, deliberately


def test_never_synced_and_different_is_a_conflict_not_a_guess(world):
    sync, remote, csv_path, db, _ = world  # noqa: RUF059 - the fixture's other parts are not needed here
    remote.rows = {"RISK-001": SEEDED[0].model_copy(update={"status": "closed"})}

    assert sync.sync().action == CONFLICT


# --- bad data and an unreachable store never corrupt the repo copy -------------------------------------------------------------


def test_an_unreachable_lead_store_changes_nothing_and_is_reported(world):
    sync, remote, csv_path, db, tmp_path = world  # noqa: RUF059 - the fixture's other parts are not needed here
    sync.sync()
    remote.down = True
    csv_before = csv_path.read_bytes()

    outcome = sync.sync()

    assert outcome.action == REMOTE_UNREACHABLE and csv_path.read_bytes() == csv_before
    assert _sqlite_risks(db) == read_risks(csv_path)  # offline: the repo copy still works


@pytest.mark.parametrize("bad", [{"severity": "urgent"}, {"status": "done"}, {"title": ""}, {"opened_at": "soon"}])
def test_an_invalid_edit_in_the_lead_store_is_refused_and_named(world, bad):
    sync, remote, csv_path, db, _ = world
    sync.sync()
    remote.lead_edits("RISK-001", **bad)
    csv_before = csv_path.read_bytes()

    outcome = sync.sync()

    assert outcome.action == INVALID_REMOTE and outcome.problems
    assert any("RISK-001" in p for p in outcome.problems)
    assert csv_path.read_bytes() == csv_before and _sqlite_risks(db) == SEEDED


def test_a_risk_pointing_at_an_item_the_tracker_does_not_have_is_refused(world):
    sync, remote, csv_path, db, _ = world  # noqa: RUF059 - the fixture's other parts are not needed here
    sync.sync()
    remote.lead_adds(_new_risk(related_item_id="PM-999"))

    outcome = sync.sync()

    assert outcome.action == INVALID_REMOTE and any("PM-999" in p for p in outcome.problems)
    assert [r.id for r in read_risks(csv_path)] == ["RISK-001", "RISK-002", "RISK-003"]


def test_a_broken_repo_csv_is_refused_and_the_lead_store_is_left_alone(world):
    sync, remote, csv_path, db, _ = world  # noqa: RUF059 - the fixture's other parts are not needed here
    sync.sync()
    csv_path.write_text("id,title\nRISK-001,only two columns\n")
    before = dict(remote.rows)

    outcome = sync.sync()

    assert outcome.action == INVALID_LOCAL and remote.rows == before


# --- dry runs write nothing ---------------------------------------------------------------------------------------------------


def test_a_dry_run_reports_what_would_happen_and_writes_nothing(world):
    sync, remote, csv_path, db, tmp_path = world
    sync.sync()
    remote.lead_edits("RISK-001", status="mitigated")
    csv_before, remote_before = csv_path.read_bytes(), dict(remote.rows)
    baseline_before = (tmp_path / "baseline.json").read_text()

    outcome = sync.sync(dry_run=True)

    assert outcome.action == PULLED and outcome.dry_run and "RISK-001" in "\n".join(outcome.changes)
    assert csv_path.read_bytes() == csv_before and remote.rows == remote_before
    assert (tmp_path / "baseline.json").read_text() == baseline_before
    assert _sqlite_risks(db) == SEEDED


def test_a_dry_run_push_writes_nothing(world):
    sync, remote, csv_path, db, _ = world  # noqa: RUF059 - the fixture's other parts are not needed here

    outcome = sync.sync(dry_run=True)

    assert outcome.action == PUSHED and outcome.dry_run and remote.rows == {}


# --- comparing the three stores ---------------------------------------------------------------------------------------------------


def test_compare_shows_the_three_stores_side_by_side(world):
    sync, remote, csv_path, db, _ = world  # noqa: RUF059 - the fixture's other parts are not needed here
    sync.sync()

    comparison = sync.compare()

    assert comparison.in_sync and comparison.counts == {"repo CSV": 3, "lead store": 3, "runtime copy": 3}


def test_compare_names_what_differs(world):
    sync, remote, csv_path, db, _ = world  # noqa: RUF059 - the fixture's other parts are not needed here
    sync.sync()
    remote.lead_edits("RISK-002", severity="low")

    comparison = sync.compare()

    assert not comparison.in_sync and any("RISK-002" in line and "severity" in line for line in comparison.differences)


def test_compare_copes_with_an_unreachable_lead_store(world):
    sync, remote, csv_path, db, _ = world  # noqa: RUF059 - the fixture's other parts are not needed here
    remote.down = True

    comparison = sync.compare()

    assert comparison.remote_reachable is False and not comparison.in_sync


# --- the runtime copy ---------------------------------------------------------------------------------------------------------------


def test_the_runtime_copy_is_replaced_not_appended_to(world):
    sync, remote, csv_path, db, _ = world  # noqa: RUF059 - the fixture's other parts are not needed here
    conn = sqlite3.connect(db)
    conn.execute("UPDATE risks SET title = 'stale hand edit' WHERE id = 'RISK-001'")
    conn.commit()
    conn.close()

    sync.sync()  # first push; the runtime copy is brought back to the repo's version

    assert _sqlite_risks(db) == read_risks(csv_path)


# --- the acceptance test ------------------------------------------------------------------------------------------------------------


def test_the_same_three_seeded_risks_are_visible_in_both_stores_and_stay_in_sync(world):
    """PM-15's acceptance: the same 3 seeded risks in the repo store AND the
    lead-facing store, and they stay in sync through edits on either side."""
    sync, remote, csv_path, db, _ = world

    sync.sync()  # fresh start: the seeded log reaches the lead's store
    repo_ids = [r.id for r in read_risks(csv_path)]
    lead_ids = [r.id for r in remote.list_risks()]
    assert repo_ids == lead_ids == ["RISK-001", "RISK-002", "RISK-003"]
    assert read_risks(csv_path) == remote.list_risks() == SEEDED  # identical in every field

    remote.lead_edits("RISK-001", status="mitigated")  # the lead edits in their table
    assert sync.sync().action == PULLED
    assert read_risks(csv_path)[0].status == "mitigated"  # ...and the repo follows

    CsvRiskLog(csv_path).update_risk("RISK-002", SEEDED[1].model_copy(update={"status": "mitigated"}))  # a repo edit
    assert sync.sync().action == PUSHED
    assert remote.rows["RISK-002"].status == "mitigated"  # ...and the lead's table follows

    assert read_risks(csv_path) == remote.list_risks() == _sqlite_risks(db)  # all three agree at the end
    assert sync.compare().in_sync


def test_a_leads_edit_changes_what_the_morning_brief_says(world):
    """The point of the lead-facing log: closing a risk there changes the brief."""
    from pm.reporting.facts import compute_morning_brief_facts
    from pm.state.snapshot import build_current_snapshot

    sync, remote, csv_path, db, _ = world  # noqa: RUF059 - the fixture's other parts are not needed here
    sync.sync()

    def blockers():
        snapshot = build_current_snapshot(db, taken_at="2026-09-16T23:59:59+00:00")
        return [b.risk_id for b in compute_morning_brief_facts(snapshot).blockers]

    assert "RISK-001" in blockers()

    remote.lead_edits("RISK-001", status="mitigated")
    sync.sync()

    assert "RISK-001" not in blockers() and "RISK-002" in blockers()


# --- the morning job pulls the lead's edits first --------------------------------------------------------------------------------------


def _job_config():
    from pm.scheduling.config import ProjectScheduleConfig
    from pm.seed.build import CHANNEL_ID

    return ProjectScheduleConfig(
        channel_id=CHANNEL_ID, timezone="Asia/Colombo", working_days=["Mon", "Tue", "Wed", "Thu", "Fri"],
        morning_brief_time=time(8, 0), end_of_day_time=time(17, 0),
    )


@pytest.fixture()
def job_world(world, monkeypatch):
    from pm.risklog import hook

    sync, remote, csv_path, db, tmp_path = world  # noqa: RUF059 - the fixture's other parts are not needed here
    monkeypatch.setenv("PM_DB_PATH", str(db))  # the job's database is "the configured one"
    monkeypatch.setattr(hook, "_build_sync", lambda db_path: sync)
    return sync, remote, csv_path, db


def _run_job(db):
    from pm.approval.service import ApprovalPolicy
    from pm.eval.pm12_cases import ScriptedGateway
    from pm.jobs.morning_brief_job import run_morning_brief_job

    return run_morning_brief_job(
        _job_config(), ScriptedGateway(), moment=datetime(2026, 9, 16, 2, 30, tzinfo=timezone.utc),
        db_path=db, policy=ApprovalPolicy(),
    )


def test_it_is_off_by_default_and_the_job_ignores_the_lead_store(job_world, monkeypatch):
    sync, remote, csv_path, db = job_world
    sync.sync()
    remote.lead_edits("RISK-001", status="mitigated")
    monkeypatch.setenv("PM_RISK_LOG_SYNC", "0")

    result = _run_job(db)

    assert read_risks(csv_path)[0].status == "open" and "RISK-001" in result.brief.content


def test_when_on_the_job_pulls_the_leads_edits_before_building_the_brief(job_world, monkeypatch):
    sync, remote, csv_path, db = job_world
    sync.sync()
    remote.lead_edits("RISK-001", status="mitigated")
    monkeypatch.setenv("PM_RISK_LOG_SYNC", "1")

    result = _run_job(db)

    assert read_risks(csv_path)[0].status == "mitigated"
    assert "RISK-001" not in result.brief.content.split("## Blockers")[1] and "RISK-002" in result.brief.content


def test_when_the_lead_store_is_down_the_job_still_runs_from_the_repo_copy(job_world, monkeypatch):
    sync, remote, csv_path, db = job_world  # noqa: RUF059 - the fixture's other parts are not needed here
    sync.sync()
    remote.down = True
    monkeypatch.setenv("PM_RISK_LOG_SYNC", "1")

    result = _run_job(db)

    assert result.status == "generated" and "RISK-001" in result.brief.content  # offline and reproducible


def test_a_conflict_does_not_stop_the_brief(job_world, monkeypatch):
    sync, remote, csv_path, db = job_world
    sync.sync()
    CsvRiskLog(csv_path).update_risk("RISK-001", SEEDED[0].model_copy(update={"severity": "high"}))
    remote.lead_edits("RISK-002", status="closed")
    monkeypatch.setenv("PM_RISK_LOG_SYNC", "1")

    result = _run_job(db)

    assert result.status == "generated"


def test_the_job_never_syncs_a_database_that_is_not_the_configured_one(world, monkeypatch, tmp_path):
    """A leaked setting must not let a test or temporary database rewrite the
    committed CSV."""
    from pm.risklog import hook

    sync, remote, csv_path, db, _ = world  # noqa: RUF059 - the fixture's other parts are not needed here
    calls = []
    monkeypatch.setattr(hook, "_build_sync", lambda db_path: calls.append(db_path) or sync)
    monkeypatch.setenv("PM_RISK_LOG_SYNC", "1")
    monkeypatch.setenv("PM_DB_PATH", str(tmp_path / "somewhere_else.db"))

    _run_job(db)

    assert calls == []
