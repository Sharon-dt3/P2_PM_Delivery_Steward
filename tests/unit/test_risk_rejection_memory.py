"""PM-17: rejection memory -- a rejected proposal is fingerprinted, so the next run does
not propose it again identically; a material change to the underlying blocker allows a
new proposal, with the change stated.

Acceptance: reject one proposal, rerun, assert no duplicate appears.

The fingerprint is computed by code from the blocker's MATERIAL facts only (blocked-since
date, assignee, title, sprint, commitments and their due dates). Time passing (the day
count) and the model's wording are not material, so neither can bring a rejected
proposal back. Which of the earlier proposals matches is decided from stored facts,
before any model is asked.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest
from spine.approval.proposals import PENDING, REJECTED, ProposalStore

from pm.adapters.commitments import Commitment
from pm.approval.service import ApprovalPolicy, reject
from pm.risk.gaps import find_gaps
from pm.risk.memory import fingerprint
from pm.risk.proposals import RISK_PROPOSAL_TYPE, detect_and_propose
from pm.risk.scripted import ScriptedRiskGateway
from pm.seed.build import ANCHOR_DATE
from pm.state.snapshot import build_current_snapshot

AS_OF = f"{ANCHOR_DATE.isoformat()}T12:00:00+00:00"
POLICY = ApprovalPolicy(approver_ids=frozenset({"sharon.silva"}))


@pytest.fixture()
def snapshot(seeded_db_path):
    return build_current_snapshot(seeded_db_path, taken_at=AS_OF, tz_name="Asia/Colombo")


def _all(db):
    store = ProposalStore(db)
    return [p for status in ("pending", "approved", "rejected", "applied") for p in store.list_by_status(status)
            if p.type == RISK_PROPOSAL_TYPE]


def _for(db, item_id):
    return sorted((p for p in _all(db) if p.payload["item_id"] == item_id), key=lambda p: p.created_at)


def _run(db, snapshot, gateway=None):
    return detect_and_propose(snapshot, gateway or ScriptedRiskGateway(find_gaps(snapshot)), db_path=db)


def _reject(db, item_id, reason="not a risk"):
    pid = next(p.id for p in _for(db, item_id) if p.status == PENDING)
    reject(pid, approver_id="sharon.silva", reason=reason, policy=POLICY, db_path=db)
    return pid


def _changed(snapshot, item_id, **updates):
    return snapshot.model_copy(update={"items": [
        i.model_copy(update=updates) if i.id == item_id else i for i in snapshot.items
    ]})


def _result(results, item_id):
    return next(r for r in results if r.item_id == item_id)


# --- the acceptance test ------------------------------------------------------------------------------------------


def test_reject_one_rerun_and_no_duplicate_appears(seeded_db_path, snapshot):
    """PM-17's acceptance."""
    _run(seeded_db_path, snapshot)
    rejected_id = _reject(seeded_db_path, "PM-014")
    before = {p.id for p in _all(seeded_db_path)}

    results = _run(seeded_db_path, snapshot)

    assert {p.id for p in _all(seeded_db_path)} == before and len(before) == 2  # nothing new, no duplicate of any kind
    assert [p.status for p in _for(seeded_db_path, "PM-014")] == [REJECTED]
    assert not any(r.created for r in results)
    r = _result(results, "PM-014")
    assert r.state == "rejected_unchanged" and r.proposal_id == rejected_id


def test_rerunning_many_times_never_brings_it_back(seeded_db_path, snapshot):
    _run(seeded_db_path, snapshot)
    _reject(seeded_db_path, "PM-014")

    for _ in range(4):
        _run(seeded_db_path, snapshot)

    assert len(_for(seeded_db_path, "PM-014")) == 1 and len(_all(seeded_db_path)) == 2


def test_the_model_is_not_asked_about_a_blocker_it_remembers_rejecting(seeded_db_path, snapshot):
    _run(seeded_db_path, snapshot)
    _reject(seeded_db_path, "PM-014")
    gateway = ScriptedRiskGateway(find_gaps(snapshot))

    _run(seeded_db_path, snapshot, gateway)

    assert gateway.calls == 0


def test_the_other_blocker_is_unaffected_by_a_rejection(seeded_db_path, snapshot):
    _run(seeded_db_path, snapshot)
    _reject(seeded_db_path, "PM-014")

    results = _run(seeded_db_path, snapshot)

    assert _result(results, "PM-015").state == "already_proposed"
    assert [p.status for p in _for(seeded_db_path, "PM-015")] == [PENDING]


def test_time_passing_alone_does_not_bring_a_rejected_proposal_back(seeded_db_path, snapshot):
    """Three days later PM-014 is blocked for 7 days instead of 4: the same blockage."""
    _run(seeded_db_path, snapshot)
    _reject(seeded_db_path, "PM-014")
    later = build_current_snapshot(seeded_db_path, taken_at="2026-09-21T12:00:00+00:00", tz_name="Asia/Colombo")
    assert find_gaps(later)[0].days_blocked == 7

    _run(seeded_db_path, later)

    assert len(_for(seeded_db_path, "PM-014")) == 1


def test_different_wording_from_the_model_does_not_bring_it_back(seeded_db_path, snapshot):
    _run(seeded_db_path, snapshot)
    _reject(seeded_db_path, "PM-014")
    chatty = ScriptedRiskGateway(find_gaps(snapshot), rewrite=lambda lines, gap: [{**l, "text": l["text"] + " It is blocked."} for l in lines],
                                 persistent=True)

    _run(seeded_db_path, snapshot, chatty)

    assert len(_for(seeded_db_path, "PM-014")) == 1


# --- the fingerprint -------------------------------------------------------------------------------------------------


def test_a_proposal_carries_the_fingerprint_of_the_blocker_it_was_about(seeded_db_path, snapshot):
    _run(seeded_db_path, snapshot)

    pm014, pm015 = (_for(seeded_db_path, i)[0] for i in ("PM-014", "PM-015"))

    gaps = {g.item_id: g for g in find_gaps(snapshot)}
    assert pm014.payload["fingerprint"] == fingerprint(gaps["PM-014"]) and re.fullmatch(r"[0-9a-f]{64}", pm014.payload["fingerprint"])
    assert pm014.payload["fingerprint"] != pm015.payload["fingerprint"]
    assert pm014.payload["material_facts"]["blocked_since"] == "2026-09-14"
    assert pm014.payload["material_facts"]["assignee_id"] == "olivia.dupree"


def test_the_fingerprint_survives_rejection(seeded_db_path, snapshot):
    _run(seeded_db_path, snapshot)
    before = _for(seeded_db_path, "PM-014")[0].payload["fingerprint"]

    _reject(seeded_db_path, "PM-014")

    assert _for(seeded_db_path, "PM-014")[0].payload["fingerprint"] == before


def test_the_fingerprint_is_stable_and_ignores_the_passing_of_time(seeded_db_path, snapshot):
    later = build_current_snapshot(seeded_db_path, taken_at="2026-09-19T12:00:00+00:00", tz_name="Asia/Colombo")

    assert [fingerprint(g) for g in find_gaps(snapshot)] == [fingerprint(g) for g in find_gaps(snapshot)]
    assert [fingerprint(g) for g in find_gaps(snapshot)] == [fingerprint(g) for g in find_gaps(later)]


def _fp(snapshot, item_id="PM-014"):
    return fingerprint(next(g for g in find_gaps(snapshot) if g.item_id == item_id))


@pytest.mark.parametrize("updates", [
    {"blocked_since": "2026-09-16"},
    {"assignee_id": "wei.chen"},
    {"assignee_id": None},
    {"title": "Search index blocked on a different migration"},
    {"sprint_id": "sprint-12"},
], ids=["blocked-since", "assignee", "unassigned", "title", "sprint"])
def test_a_material_change_to_the_blocker_changes_the_fingerprint(snapshot, updates):
    assert _fp(_changed(snapshot, "PM-014", **updates)) != _fp(snapshot)


def _with_commitments(snapshot, commitments):
    return snapshot.model_copy(update={"commitments": commitments})


def _commitment(**kw):
    base = {"id": 6, "member_id": "olivia.dupree", "item_id": "PM-014", "text": "Expect the migration to clear by Wednesday",
            "due_date_iso": "2026-09-16", "made_at": "2026-09-12"}
    return Commitment(**{**base, **kw})


@pytest.mark.parametrize("change", ["due date moved", "new commitment", "commitment dropped", "commitment reworded"])
def test_a_change_in_the_commitments_changes_the_fingerprint(snapshot, change):
    base = [_commitment()]
    changed = {
        "due date moved": [_commitment(due_date_iso="2026-09-23")],
        "new commitment": [_commitment(), _commitment(id=99, text="Will escalate to the platform team")],
        "commitment dropped": [],
        "commitment reworded": [_commitment(text="Expect the migration to clear by Friday")],
    }[change]

    assert _fp(_with_commitments(snapshot, changed)) != _fp(_with_commitments(snapshot, base))


def test_things_that_are_not_material_do_not_change_the_fingerprint(snapshot):
    """A new commit or chat message on the blocker, or a changed risk elsewhere, is activity, not a change to what the blocker is."""
    base = _fp(snapshot)

    assert _fp(snapshot.model_copy(update={"commits": snapshot.commits[:-1]})) == base
    assert _fp(snapshot.model_copy(update={"risks": []})) == base


# --- a material change allows a new proposal, with the change stated ---------------------------------------------------


def test_a_material_change_after_a_rejection_is_proposed_again_with_the_change_stated(seeded_db_path, snapshot):
    _run(seeded_db_path, snapshot)
    rejected_id = _reject(seeded_db_path, "PM-014", reason="already handled in the sprint review")
    reblocked = _changed(snapshot, "PM-014", blocked_since="2026-09-18")

    results = _run(seeded_db_path, reblocked)

    again = _result(results, "PM-014")
    assert again.created and again.state == "changed_since_rejection" and again.proposal_id != rejected_id
    old, new = _for(seeded_db_path, "PM-014")
    assert old.id == rejected_id and old.status == REJECTED  # the rejection stands, untouched
    assert new.status == PENDING and new.payload["fingerprint"] != old.payload["fingerprint"]
    change = new.payload["change"]
    assert change["previous_proposal_id"] == rejected_id and change["previous_status"] == REJECTED
    assert {"field": "blocked_since", "was": "2026-09-14", "now": "2026-09-18"}.items() <= next(
        d for d in change["differences"] if d["field"] == "blocked_since").items()
    assert "blocked since" in change["text"] and "2026-09-14" in change["text"] and "2026-09-18" in change["text"]


def test_the_change_is_stated_in_the_text_a_person_reads(seeded_db_path, snapshot):
    _run(seeded_db_path, snapshot)
    rejected_id = _reject(seeded_db_path, "PM-014", reason="already handled in the sprint review")

    _run(seeded_db_path, _changed(snapshot, "PM-014", blocked_since="2026-09-18"))

    content = _for(seeded_db_path, "PM-014")[1].payload["content"]
    assert "Changed since the rejected proposal" in content and rejected_id[:8] in content
    assert "blocked since" in content and "2026-09-14" in content
    assert "sharon.silva" in content and "already handled in the sprint review" in content  # who rejected it, and why


def test_an_unchanged_first_proposal_states_no_change(seeded_db_path, snapshot):
    _run(seeded_db_path, snapshot)

    proposal = _for(seeded_db_path, "PM-014")[0]

    assert proposal.payload["change"] is None and "Changed since" not in proposal.payload["content"]


def test_a_changed_assignee_is_stated_by_name(seeded_db_path, snapshot):
    _run(seeded_db_path, snapshot)
    _reject(seeded_db_path, "PM-014")

    _run(seeded_db_path, _changed(snapshot, "PM-014", assignee_id="wei.chen"))

    new = _for(seeded_db_path, "PM-014")[1]
    text = new.payload["change"]["text"]
    assert "Olivia Dupree" in text and "Wei Chen" in text and new.payload["suggested_owner"]["id"] == "wei.chen"


def test_a_moved_commitment_is_stated(seeded_db_path, snapshot):
    snapshot = _with_commitments(snapshot, [_commitment()])
    _run(seeded_db_path, snapshot)
    _reject(seeded_db_path, "PM-014")

    _run(seeded_db_path, _with_commitments(snapshot, [_commitment(due_date_iso="2026-09-23")]))

    text = _for(seeded_db_path, "PM-014")[1].payload["change"]["text"]
    assert "commitment 6" in text and "2026-09-16" in text and "2026-09-23" in text


def test_every_difference_is_listed_when_several_things_changed(seeded_db_path, snapshot):
    _run(seeded_db_path, snapshot)
    _reject(seeded_db_path, "PM-014")

    _run(seeded_db_path, _changed(snapshot, "PM-014", blocked_since="2026-09-18", assignee_id="wei.chen"))

    fields = {d["field"] for d in _for(seeded_db_path, "PM-014")[1].payload["change"]["differences"]}
    assert fields == {"blocked_since", "assignee_id"}


def test_the_statement_of_change_is_code_not_model_prose(seeded_db_path, snapshot):
    """Even a model that tries to say something about the change cannot alter what is stated."""
    _run(seeded_db_path, snapshot)
    _reject(seeded_db_path, "PM-014")
    liar = ScriptedRiskGateway(find_gaps(snapshot), rewrite=lambda lines, gap: [{**l, "text": l["text"] + " Nothing has changed."} for l in lines],
                               persistent=True)

    _run(seeded_db_path, _changed(snapshot, "PM-014", blocked_since="2026-09-18"), liar)

    new = _for(seeded_db_path, "PM-014")[1]
    assert "Nothing has changed" not in json.dumps(new.payload) and new.payload["change"]["differences"]


def test_a_re_proposal_is_audited_with_what_it_follows(seeded_db_path, snapshot):
    import sqlite3

    _run(seeded_db_path, snapshot)
    rejected_id = _reject(seeded_db_path, "PM-014")

    _run(seeded_db_path, _changed(snapshot, "PM-014", blocked_since="2026-09-18"))

    conn = sqlite3.connect(seeded_db_path)
    rows = conn.execute("SELECT details FROM audit WHERE action = 'proposal.created' ORDER BY id").fetchall()
    conn.close()
    assert json.loads(rows[-1][0])["follows_rejected"] == rejected_id


# --- the memory is long: it covers every earlier rejection --------------------------------------------------------------


def test_a_change_that_is_then_rejected_is_remembered_too(seeded_db_path, snapshot):
    _run(seeded_db_path, snapshot)
    _reject(seeded_db_path, "PM-014")
    reblocked = _changed(snapshot, "PM-014", blocked_since="2026-09-18")
    _run(seeded_db_path, reblocked)
    _reject(seeded_db_path, "PM-014")

    _run(seeded_db_path, reblocked)

    assert [p.status for p in _for(seeded_db_path, "PM-014")] == [REJECTED, REJECTED]


def test_going_back_to_a_state_that_was_already_rejected_is_not_proposed_again(seeded_db_path, snapshot):
    _run(seeded_db_path, snapshot)
    _reject(seeded_db_path, "PM-014")
    _run(seeded_db_path, _changed(snapshot, "PM-014", blocked_since="2026-09-18"))
    _reject(seeded_db_path, "PM-014")

    results = _run(seeded_db_path, snapshot)  # back to the original facts

    assert len(_for(seeded_db_path, "PM-014")) == 2 and _result(results, "PM-014").state == "rejected_unchanged"


def test_a_proposal_still_waiting_is_not_duplicated_when_the_facts_move(seeded_db_path, snapshot):
    """Nobody has decided the first one yet: a second pending proposal for the same blocker would be a duplicate."""
    _run(seeded_db_path, snapshot)

    results = _run(seeded_db_path, _changed(snapshot, "PM-014", blocked_since="2026-09-18"))

    assert len(_for(seeded_db_path, "PM-014")) == 1
    r = _result(results, "PM-014")
    assert not r.created and r.state == "awaiting_decision"


def test_once_the_waiting_one_is_rejected_the_changed_facts_are_proposed(seeded_db_path, snapshot):
    _run(seeded_db_path, snapshot)
    reblocked = _changed(snapshot, "PM-014", blocked_since="2026-09-18")
    _run(seeded_db_path, reblocked)  # held back: the first is still pending
    _reject(seeded_db_path, "PM-014")

    results = _run(seeded_db_path, reblocked)

    assert _result(results, "PM-014").state == "changed_since_rejection" and len(_for(seeded_db_path, "PM-014")) == 2


# --- proposals made before the fingerprint existed -----------------------------------------------------------------------


def _legacy_rejected(db, item_id="PM-014", blocked_since="2026-09-14", owner="olivia.dupree"):
    """What PM-16 stored: no fingerprint, an older key, only the blocked-since date and the owner."""
    store = ProposalStore(db)
    legacy = store.create(
        type=RISK_PROPOSAL_TYPE,
        payload={"blocker_ref": f"item:{item_id}", "item_id": item_id, "description": "d", "impact": "i",
                 "suggested_owner": {"id": owner, "name": "Olivia Dupree", "evidence": "e"} if owner else None,
                 "facts": {"blocked_since": blocked_since}, "content": "c"},
        original_model_output={"description": "d", "impact": "i", "lines": [], "dropped": []},
        source_refs=[f"item:{item_id}"], idempotency_key=f"risk_log_entry:{item_id}:{blocked_since}",
    )
    reject(legacy.id, approver_id="sharon.silva", policy=POLICY, db_path=db)
    return legacy.id


def test_a_proposal_rejected_before_fingerprints_existed_is_still_remembered(seeded_db_path, snapshot):
    legacy_id = _legacy_rejected(seeded_db_path)

    results = _run(seeded_db_path, snapshot)

    assert _result(results, "PM-014").state == "rejected_unchanged" and _result(results, "PM-014").proposal_id == legacy_id
    assert [p.id for p in _for(seeded_db_path, "PM-014")] == [legacy_id]
    assert [p.status for p in _for(seeded_db_path, "PM-015")] == [PENDING]


def test_a_legacy_rejection_does_not_hide_a_material_change(seeded_db_path, snapshot):
    legacy_id = _legacy_rejected(seeded_db_path)

    results = _run(seeded_db_path, _changed(snapshot, "PM-014", blocked_since="2026-09-18"))

    r = _result(results, "PM-014")
    assert r.created and r.state == "changed_since_rejection"
    new = _for(seeded_db_path, "PM-014")[1]
    assert new.payload["change"]["previous_proposal_id"] == legacy_id and "2026-09-14" in new.payload["change"]["text"]


# --- the command line and the job --------------------------------------------------------------------------------------------


@pytest.fixture()
def cli():
    path = Path(__file__).resolve().parents[2] / "scripts" / "detect_risks.py"
    spec = importlib.util.spec_from_file_location("detect_risks_script_17", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ARGS = ["--at", "2026-09-18T12:00", "--gateway", "scripted"]


def test_the_command_says_it_remembers_the_rejection(cli, seeded_db_path, capsys):
    cli.main(["--db", str(seeded_db_path), *ARGS])
    _reject(seeded_db_path, "PM-014")
    capsys.readouterr()

    code = cli.main(["--db", str(seeded_db_path), *ARGS])

    out = capsys.readouterr().out
    assert code == 0 and "rejected earlier" in out.lower() and "not proposed again" in out.lower()
    assert len(_all(seeded_db_path)) == 2


def test_a_dry_run_tells_you_what_would_and_would_not_be_proposed(cli, seeded_db_path, capsys):
    cli.main(["--db", str(seeded_db_path), *ARGS])
    _reject(seeded_db_path, "PM-014")
    capsys.readouterr()

    cli.main(["--db", str(seeded_db_path), "--at", "2026-09-18T12:00", "--dry-run"])

    out = capsys.readouterr().out
    lines = out.splitlines()
    at = next(i for i, line in enumerate(lines) if "PM-014" in line and "Search index" in line)
    assert "rejected earlier" in lines[at + 1].lower() and "not proposed again" in lines[at + 1].lower()
    assert "already proposed" in lines[at + 3].lower()  # PM-015 is still open, untouched by PM-014's rejection
    assert len(_all(seeded_db_path)) == 2


def test_the_command_states_a_change_when_it_proposes_again(cli, seeded_db_path, capsys):
    cli.main(["--db", str(seeded_db_path), *ARGS])
    _reject(seeded_db_path, "PM-014")
    conn = __import__("sqlite3").connect(seeded_db_path)
    conn.execute("UPDATE items SET blocked_since = '2026-09-17T09:00:00+00:00' WHERE id = 'PM-014'")
    conn.commit()
    conn.close()
    capsys.readouterr()

    cli.main(["--db", str(seeded_db_path), *ARGS])

    out = capsys.readouterr().out
    assert "changed since" in out.lower() and "2026-09-14" in out


def test_the_morning_job_does_not_bring_a_rejected_proposal_back(seeded_db_path, monkeypatch):
    from datetime import datetime, time, timezone

    from pm.eval.pm12_cases import ScriptedGateway
    from pm.jobs.morning_brief_job import run_morning_brief_job
    from pm.scheduling.config import ProjectScheduleConfig
    from pm.seed.build import CHANNEL_ID

    monkeypatch.setenv("PM_RISK_DETECTION", "1")
    config = ProjectScheduleConfig(channel_id=CHANNEL_ID, timezone="Asia/Colombo", working_days=["Mon", "Tue", "Wed", "Thu", "Fri"],
                                   morning_brief_time=time(8, 0), end_of_day_time=time(17, 0))

    class Both:
        def __init__(self):
            snap = build_current_snapshot(seeded_db_path, taken_at="2026-09-18T02:30:00+00:00", tz_name="Asia/Colombo")
            self.brief, self.risk = ScriptedGateway(), ScriptedRiskGateway(find_gaps(snap))

        def generate(self, prompt, **kwargs):
            return (self.risk if "reference_id: item:" in prompt and "risk log entry" in prompt.lower() else self.brief).generate(prompt, **kwargs)

    def run(day):
        return run_morning_brief_job(config, Both(), moment=datetime(2026, 9, day, 2, 30, tzinfo=timezone.utc),
                                     db_path=seeded_db_path, policy=ApprovalPolicy())

    first = run(18)
    _reject(seeded_db_path, "PM-014")

    second = run(21)  # the next working day: PM-014 is 3 days older, still the same blockage
    assert first.risk_proposals == 2 and second.risk_proposals == 0 and len(_all(seeded_db_path)) == 2
