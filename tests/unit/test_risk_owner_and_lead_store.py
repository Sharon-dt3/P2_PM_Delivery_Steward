"""Two follow-ons to approving a risk-log proposal (tests/unit/test_risk_approval.py): the owner, and the lead's table.

OWNER. The risk log gained an optional `owner` field. A risk proposal carries a SUGGESTED owner "only where evidenced" (PM-16): the
tracker's assignee of the item. Approving the proposal writes it onto the entry as "Name (id)" (two people can share a first name), and the
audit keeps the evidence it rests on. An entry with no evidenced owner has none; nothing is guessed. The field is a real one: it is in the
CSV (the column appears only when some entry has an owner, so a log without owners is byte-for-byte as before), the runtime table, the
lead-facing table, and the three-way sync, which still fingerprints an owner-less log exactly as it always did.

LEAD'S TABLE. PM-15 wants the lead-facing store and the repo log to stay in sync. An approved write now reaches the lead's table straight
away when the sync is on (the same sync the morning job runs: only the repo moved, so it is pushed), and when it cannot (the table is
unreachable, or the lead edited it since the last sync) the write still stands in the CSV, the system of record, the audit says the lead's
table is behind and why, and the next sync brings it level. Nothing the approval refuses ever touches the lead's table.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3

import pytest
from spine.approval.proposals import ProposalStore
from spine.storage.db import run_migrations
from test_risk_log_sync import (
    InMemoryRemote,  # a sibling test module: tests/unit is on the path, but is not a package
)

from pm.adapters.risk_log import Risk, RiskLogMock
from pm.adapters.tracker import TrackerMock
from pm.api import risk_view
from pm.approval import service
from pm.approval.audit import audit_trail
from pm.channel.batches import TrackerView, consume
from pm.risk.gaps import find_gaps
from pm.risk.proposals import detect_and_propose
from pm.risk.scripted import ScriptedRiskGateway
from pm.risklog import hook
from pm.risklog.csv_store import CsvRiskLog, live_risk_log_path, read_risks
from pm.risklog.sync import PULLED, PUSHED, RiskLogSync, fingerprint
from pm.seed.build import ANCHOR_DATE, SEED_RISK_LOG_PATH
from pm.state.snapshot import build_current_snapshot
from pm.storage.db import MIGRATIONS_DIR

AS_OF = f"{ANCHOR_DATE.isoformat()}T12:00:00+00:00"
APPROVERS = service.ApprovalPolicy(approver_ids=frozenset({"sharon.silva"}))
CHANNEL = "19:proj-gamma@thread.tacv2"


def record(*blockers) -> dict:
    return {
        "schema_version": "1.0", "channel_id": CHANNEL, "channel_display_name": "Project Gamma", "date": "2026-09-18", "allowlisted": True,
        "roster": [], "generated_at": "2026-09-18T11:30:00+00:00", "participation": [], "decisions": [], "questions": [], "updates": [],
        "blockers": [{"message_id": m, "text": t, "quote": None} for m, t in blockers],
    }


def propose_batch(db, tmp_path, *blockers) -> str:
    path = tmp_path / "record.json"
    path.write_text(json.dumps(record(*blockers)), encoding="utf-8")
    view = TrackerView.from_adapters(TrackerMock(db_path=db), RiskLogMock(db_path=db))
    return consume(path, view, db_path=db).risk.proposal_id


@pytest.fixture()
def gaps(seeded_db_path) -> dict[str, str]:
    snapshot = build_current_snapshot(seeded_db_path, taken_at=AS_OF, tz_name="Asia/Colombo")
    results = detect_and_propose(snapshot, ScriptedRiskGateway(find_gaps(snapshot)), db_path=seeded_db_path)
    return {r.item_id: r.proposal_id for r in results}


def approve(db, proposal_id, **kw):
    return service.approve_and_send(proposal_id, approver_id="sharon.silva", policy=APPROVERS, db_path=db, **kw)


def applied_details(db, proposal_id) -> dict:
    return next(e for e in audit_trail(proposal_id, db_path=db).events if e["action"] == "proposal.applied")["details"]


def assignee_label(db, item_id) -> str | None:
    tracker = TrackerMock(db_path=db)
    assignee = tracker.get_item(item_id).assignee_id
    name = {a.id: a.display_name for a in tracker.list_assignees()}.get(assignee)
    return f"{name} ({assignee})" if assignee else None


# --- the owner ---------------------------------------------------------------------------------------------------------------


def test_approving_a_gap_writes_the_evidenced_owner_as_name_and_id_and_keeps_the_evidence(seeded_db_path, gaps):
    approve(seeded_db_path, gaps["PM-014"])

    (entry,) = [r for r in CsvRiskLog(live_risk_log_path()).list_risks() if r.related_item_id == "PM-014"]
    assert entry.owner == assignee_label(seeded_db_path, "PM-014") == "Olivia Dupree (olivia.dupree)"  # not Olivia Dupont
    (owner,) = applied_details(seeded_db_path, gaps["PM-014"])["owners"]
    assert owner == {"risk_id": entry.id, "owner": entry.owner, "evidence": "assignee of PM-014 in the tracker"}


def test_the_owner_is_in_the_csv_and_the_runtime_copy_the_detector_reads(seeded_db_path, gaps):
    approve(seeded_db_path, gaps["PM-014"])

    header = live_risk_log_path().read_text(encoding="utf-8").splitlines()[0]
    assert header.endswith(",owner")
    assert [r.owner for r in RiskLogMock(seeded_db_path).list_risks() if r.id == "RISK-004"] == ["Olivia Dupree (olivia.dupree)"]


def test_a_batch_item_on_a_tracker_item_gets_its_assignees_name_resolved(seeded_db_path, tmp_path):
    pid = propose_batch(seeded_db_path, tmp_path, ("m4", "PM-014 is still blocked on the staging migration."))

    approve(seeded_db_path, pid)

    (entry,) = [r for r in CsvRiskLog(live_risk_log_path()).list_risks() if r.id == "RISK-004"]
    assert entry.owner == "Olivia Dupree (olivia.dupree)"
    assert applied_details(seeded_db_path, pid)["owners"][0]["evidence"] == "assignee of PM-014 in the tracker"


def test_an_entry_with_no_evidenced_owner_has_none_and_the_csv_has_no_owner_column(seeded_db_path, tmp_path):
    pid = propose_batch(seeded_db_path, tmp_path, ("m6", "The adapter payload shape is undocumented."))  # names no tracker item

    approve(seeded_db_path, pid)

    (entry,) = [r for r in CsvRiskLog(live_risk_log_path()).list_risks() if r.id == "RISK-004"]
    assert entry.owner is None and applied_details(seeded_db_path, pid)["owners"] == []
    assert live_risk_log_path().read_text(encoding="utf-8").splitlines()[0] == "id,title,description,severity,status,related_item_id,opened_at"


def test_an_unassigned_item_is_not_given_an_owner_by_guessing(seeded_db_path):
    conn = sqlite3.connect(seeded_db_path)
    conn.execute("UPDATE items SET assignee_id = NULL WHERE id = 'PM-014'")  # nobody has it, so there is no evidence of an owner
    conn.commit()
    conn.close()
    snapshot = build_current_snapshot(seeded_db_path, taken_at=AS_OF, tz_name="Asia/Colombo")
    proposals = {r.item_id: r.proposal_id for r in detect_and_propose(snapshot, ScriptedRiskGateway(find_gaps(snapshot)), db_path=seeded_db_path)}
    assert ProposalStore(seeded_db_path).get(proposals["PM-014"]).payload["suggested_owner"] is None

    approve(seeded_db_path, proposals["PM-014"])

    (entry,) = [r for r in CsvRiskLog(live_risk_log_path()).list_risks() if r.related_item_id == "PM-014"]
    assert entry.owner is None and applied_details(seeded_db_path, proposals["PM-014"])["owners"] == []


def test_the_explanation_names_the_owner_when_the_entry_has_one():
    risk = Risk(id="RISK-004", title="T", description="D", severity="high", status="open", related_item_id=None, opened_at="2026-09-18",
                owner="Olivia Dupree (olivia.dupree)")

    assert "Its owner is Olivia Dupree (olivia.dupree)." in risk_view.explain_risk(risk, None, None, [])
    assert "owner" not in risk_view.explain_risk(risk.model_copy(update={"owner": None}), None, None, [])


# --- the owner is a real field everywhere: nothing that existed breaks -------------------------------------------------------


def test_a_log_with_no_owners_fingerprints_exactly_as_it_did_before_the_field_existed():
    risks = read_risks(SEED_RISK_LOG_PATH)
    before_the_field = json.dumps(
        [{k: v for k, v in r.model_dump().items() if k != "owner"} for r in sorted(risks, key=lambda r: r.id)], sort_keys=True)

    assert fingerprint(risks) == hashlib.sha256(before_the_field.encode("utf-8")).hexdigest()  # a baseline recorded earlier is still valid
    assert fingerprint([risks[0].model_copy(update={"owner": "Sam"}), *risks[1:]]) != fingerprint(risks)  # but an owner is a change


def test_a_database_made_before_the_owner_column_reads_fine_and_is_brought_up_to_date_when_written(tmp_path):
    older = tmp_path / "older_migrations"
    older.mkdir()
    for migration in MIGRATIONS_DIR.glob("*.sql"):
        if not migration.name.startswith("0006"):
            shutil.copy(migration, older / migration.name)
    db = tmp_path / "older.db"
    run_migrations(db, older)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO risks (id, title, description, severity, status, related_item_id, opened_at) "
                 "VALUES ('RISK-001', 'T', 'D', 'low', 'open', NULL, '2026-09-01')")
    conn.commit()
    conn.close()
    log = RiskLogMock(db)

    assert [r.owner for r in log.list_risks()] == [None]  # reading needs no new column
    log.create_risk(Risk(id="RISK-002", title="T2", description="D", severity="low", status="open", related_item_id=None,
                         opened_at="2026-09-02", owner="Sam (sam)"))

    assert {r.id: r.owner for r in log.list_risks()} == {"RISK-001": None, "RISK-002": "Sam (sam)"}


@pytest.fixture()
def sync_world(seeded_db_path, tmp_path, monkeypatch):
    """The lead's table as a fake, the sync wired as the morning job wires it, the sync switched on, the first sync done."""
    remote = InMemoryRemote()
    sync = RiskLogSync(CsvRiskLog(live_risk_log_path()), remote, db_path=seeded_db_path, baseline_path=tmp_path / "baseline.json")
    sync.sync()  # the agreed starting state: the three seeded risks in both places
    remote.sync_calls = []  # every time the approval path reached for the sync
    monkeypatch.setattr(hook, "_build_sync", lambda db_path: remote.sync_calls.append(db_path) or sync)
    monkeypatch.setenv("PM_DB_PATH", str(seeded_db_path))  # this database is "the configured one"
    monkeypatch.setenv("PM_RISK_LOG_SYNC", "1")
    return sync, remote


def test_the_owner_travels_through_the_sync_both_ways(seeded_db_path, gaps, sync_world):
    sync, remote = sync_world
    approve(seeded_db_path, gaps["PM-014"])
    assert remote.rows["RISK-004"].owner == "Olivia Dupree (olivia.dupree)"  # approval -> the lead's table

    remote.lead_edits("RISK-004", owner="Wei Chen (wei.chen)")  # the lead reassigns it in the table
    assert sync.sync().action == PULLED

    assert CsvRiskLog(live_risk_log_path()).get_risk("RISK-004").owner == "Wei Chen (wei.chen)"  # and it reaches the repo log
    assert RiskLogMock(seeded_db_path).get_risk("RISK-004").owner == "Wei Chen (wei.chen)"  # and the runtime copy


# --- the lead's table -------------------------------------------------------------------------------------------------------


def test_an_approved_write_reaches_the_leads_table_straight_away(seeded_db_path, gaps, sync_world):
    _, remote = sync_world
    writes_before = remote.writes

    result = approve(seeded_db_path, gaps["PM-014"])

    assert result.outcome == service.APPLIED_OUTCOME and "the lead's table is up to date" in result.detail
    assert "RISK-004" in remote.rows and remote.writes == writes_before + 1  # pushed once
    assert len(remote.sync_calls) == 1  # one sync for one approval
    assert applied_details(seeded_db_path, gaps["PM-014"])["lead_store"] == PUSHED


def test_a_batch_of_several_entries_reaches_the_leads_table_in_one_push(seeded_db_path, tmp_path, sync_world):
    _, remote = sync_world
    pid = propose_batch(seeded_db_path, tmp_path, ("m4", "PM-014 is still blocked on the staging migration."),
                        ("m6", "The adapter payload shape is undocumented."))
    writes_before = remote.writes

    approve(seeded_db_path, pid)

    assert {"RISK-004", "RISK-005"} <= set(remote.rows) and remote.writes == writes_before + 1


def test_when_the_leads_table_is_unreachable_the_write_stands_and_the_next_sync_brings_it_level(seeded_db_path, gaps, sync_world):
    sync, remote = sync_world
    remote.down = True

    result = approve(seeded_db_path, gaps["PM-014"])

    assert result.outcome == service.APPLIED_OUTCOME and "NOT up to date yet (remote_unreachable)" in result.detail
    assert "RISK-004" in {r.id for r in CsvRiskLog(live_risk_log_path()).list_risks()}  # the system of record has it
    assert applied_details(seeded_db_path, gaps["PM-014"])["lead_store"] == "remote_unreachable"
    assert ProposalStore(seeded_db_path).get(gaps["PM-014"]).status == "applied"  # not a failed approval

    remote.down = False
    assert sync.sync().action == PUSHED and "RISK-004" in remote.rows  # the next sync (the morning job's) heals it


def test_when_the_lead_edited_the_table_meanwhile_nothing_of_theirs_is_overwritten(seeded_db_path, gaps, sync_world):
    _, remote = sync_world
    remote.lead_edits("RISK-001", status="mitigated")  # the lead moved since the last sync

    result = approve(seeded_db_path, gaps["PM-014"])

    assert result.outcome == service.APPLIED_OUTCOME and "NOT up to date yet (conflict)" in result.detail
    assert remote.rows["RISK-001"].status == "mitigated" and "RISK-004" not in remote.rows  # their edit is intact, ours waits
    assert "RISK-004" in {r.id for r in CsvRiskLog(live_risk_log_path()).list_risks()}
    assert applied_details(seeded_db_path, gaps["PM-014"])["lead_store"] == "conflict"


def test_with_the_sync_off_the_approval_does_not_touch_the_leads_table_or_mention_it(seeded_db_path, gaps, sync_world, monkeypatch):
    _, remote = sync_world
    monkeypatch.setenv("PM_RISK_LOG_SYNC", "0")
    writes_before = remote.writes

    result = approve(seeded_db_path, gaps["PM-014"])

    assert result.outcome == service.APPLIED_OUTCOME and "lead's table" not in result.detail
    assert remote.writes == writes_before and applied_details(seeded_db_path, gaps["PM-014"])["lead_store"] == "off"


def test_a_database_that_is_not_the_configured_one_never_syncs(seeded_db_path, gaps, sync_world, monkeypatch, tmp_path):
    _, remote = sync_world
    monkeypatch.setenv("PM_DB_PATH", str(tmp_path / "somewhere_else.db"))
    writes_before = remote.writes

    approve(seeded_db_path, gaps["PM-014"])

    assert remote.writes == writes_before and applied_details(seeded_db_path, gaps["PM-014"])["lead_store"] == "skipped"


def test_a_sync_that_blows_up_does_not_undo_the_write(seeded_db_path, gaps, sync_world, monkeypatch):
    def broken(db_path):
        raise RuntimeError("the sync is broken")

    monkeypatch.setattr(hook, "_build_sync", broken)

    result = approve(seeded_db_path, gaps["PM-014"])

    assert result.outcome == service.APPLIED_OUTCOME and "RISK-004" in {r.id for r in CsvRiskLog(live_risk_log_path()).list_risks()}
    assert applied_details(seeded_db_path, gaps["PM-014"])["lead_store"] == "error"


@pytest.mark.parametrize("why", ["a stranger", "a bad severity", "an edited approval", "an unknown proposal"])
def test_anything_the_approval_refuses_never_touches_the_leads_table(seeded_db_path, gaps, sync_world, why):
    _, remote = sync_world
    writes_before = remote.writes

    result = {
        "a stranger": lambda: service.approve_and_send(gaps["PM-014"], approver_id="mallory", policy=APPROVERS, db_path=seeded_db_path),
        "a bad severity": lambda: approve(seeded_db_path, gaps["PM-014"], severity="catastrophic"),
        "an edited approval": lambda: approve(seeded_db_path, gaps["PM-014"], edited_content="something else"),
        "an unknown proposal": lambda: approve(seeded_db_path, "no-such-proposal"),
    }[why]()

    assert result.outcome == service.REFUSED and remote.writes == writes_before and "RISK-004" not in remote.rows
    assert remote.sync_calls == []  # a refusal does not even look at the lead's table


def test_a_rejection_never_touches_the_leads_table_either(seeded_db_path, gaps, sync_world):
    _, remote = sync_world
    writes_before = remote.writes

    service.reject(gaps["PM-014"], approver_id="sharon.silva", reason="tracked elsewhere", policy=APPROVERS, db_path=seeded_db_path)

    assert remote.writes == writes_before and remote.sync_calls == []
