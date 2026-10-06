"""PM-16: one proposal per current blocker that is missing from the risk log.

Arithmetic in code, prose from the model: the gap set, the durations and the owner come
from Python; the model only phrases the description and the impact, and every line it
writes is held to the facts. Each proposal carries the description, the impact, a
suggested owner ONLY where the tracker evidences one, and the blocker reference that
grounds it.

Acceptance test: the two missing blockers (PM-014, PM-015) are proposed; the ones
already in the risk log (PM-023 -> RISK-001, PM-024 -> RISK-002, and RISK-003) are not.
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from spine.approval.proposals import PENDING, REJECTED, ProposalStore
from spine.llm.gateway import LLMResponse

from pm.approval.service import ApprovalPolicy, reject
from pm.risk.gaps import find_gaps
from pm.risk.proposals import RISK_PROPOSAL_TYPE, detect_and_propose
from pm.risk.scripted import ScriptedRiskGateway
from pm.seed.build import ANCHOR_DATE
from pm.state.snapshot import (
    ChannelSnapshot,
    NormalizedItem,
    ProjectSnapshot,
    build_current_snapshot,
)

AS_OF = f"{ANCHOR_DATE.isoformat()}T12:00:00+00:00"


@pytest.fixture()
def snapshot(seeded_db_path):
    return build_current_snapshot(seeded_db_path, taken_at=AS_OF, tz_name="Asia/Colombo")


def _proposals(db):
    store = ProposalStore(db)
    return [p for status in ("pending", "approved", "rejected", "applied") for p in store.list_by_status(status)
            if p.type == RISK_PROPOSAL_TYPE]


def _by_item(db):
    return {p.payload["item_id"]: p for p in _proposals(db)}


def _run(db, snapshot, gateway=None, **kwargs):
    return detect_and_propose(snapshot, gateway or ScriptedRiskGateway(find_gaps(snapshot)), db_path=db, **kwargs)


# --- the acceptance test ----------------------------------------------------------------------------------------


def test_the_two_missing_blockers_are_proposed_and_the_ones_already_logged_are_not(seeded_db_path, snapshot):
    """PM-16's acceptance: PM-014 and PM-015 get a proposal; PM-023 (RISK-001), PM-024
    (RISK-002) and RISK-003 do not."""
    results = _run(seeded_db_path, snapshot)

    proposals = _proposals(seeded_db_path)
    assert sorted(p.payload["blocker_ref"] for p in proposals) == ["item:PM-014", "item:PM-015"]
    assert [r.item_id for r in results] == ["PM-014", "PM-015"] and all(r.created for r in results)
    text = json.dumps([p.payload for p in proposals])
    for already_logged in ("PM-023", "PM-024", "RISK-001", "RISK-002", "RISK-003"):
        assert already_logged not in text, already_logged
    assert all(p.status == PENDING for p in proposals)  # proposals only: nobody has approved anything


# --- what a proposal contains ---------------------------------------------------------------------------------------


def test_a_proposal_has_a_description_an_impact_an_owner_and_the_blocker_reference(seeded_db_path, snapshot):
    _run(seeded_db_path, snapshot)

    payload = _by_item(seeded_db_path)["PM-014"].payload

    assert payload["blocker_ref"] == "item:PM-014" and payload["item_id"] == "PM-014"
    assert payload["description"] and payload["impact"]
    assert payload["suggested_owner"] == {
        "id": "olivia.dupree", "name": "Olivia Dupree", "evidence": "assignee of PM-014 in the tracker",
    }
    assert "Search index blocked on staging DB migration" in payload["description"]
    assert "4 days" in payload["impact"] and "2 days left" in payload["impact"] and "2 days overdue" in payload["impact"]


def test_the_proposal_shows_its_evidence_so_a_person_can_check_it(seeded_db_path, snapshot):
    _run(seeded_db_path, snapshot)

    proposal = _by_item(seeded_db_path)["PM-014"]

    assert "item:PM-014" in proposal.source_refs
    evidence = proposal.payload["evidence"]
    assert evidence[0]["ref"] == "item:PM-014" and "4 days as of 2026-09-18" in evidence[0]["text"]
    assert proposal.payload["facts"] == {
        "as_of": "2026-09-18", "blocked_since": "2026-09-14", "days_blocked": 4, "sprint_days_left": 2,
    }


def test_the_original_model_output_is_kept_apart_from_the_final_payload(seeded_db_path, snapshot):
    _run(seeded_db_path, snapshot)

    proposal = _by_item(seeded_db_path)["PM-014"]

    assert proposal.original_model_output["description"] == proposal.payload["description"]
    assert proposal.original_model_output["lines"] and all(
        line["reference_id"] == "item:PM-014" and line["quote"] for line in proposal.original_model_output["lines"]
    )


def test_a_readable_summary_is_there_for_the_dashboard(seeded_db_path, snapshot):
    _run(seeded_db_path, snapshot)

    payload = _by_item(seeded_db_path)["PM-014"].payload

    for needed in ("PM-014", "Olivia Dupree", "assignee of PM-014", payload["description"], payload["impact"]):
        assert needed in payload["content"], needed
    assert payload["local_date"] == "2026-09-18" and payload["target_channel"]


def test_nothing_is_sent_or_changed_by_proposing(seeded_db_path, snapshot):
    conn = sqlite3.connect(seeded_db_path)
    risks_before = conn.execute("SELECT * FROM risks ORDER BY id").fetchall()
    conn.close()

    _run(seeded_db_path, snapshot)

    conn = sqlite3.connect(seeded_db_path)
    assert conn.execute("SELECT * FROM risks ORDER BY id").fetchall() == risks_before  # the risk log itself is untouched
    assert conn.execute("SELECT count(*) FROM write_log").fetchone()[0] == 0
    conn.close()


def test_creation_is_audited_as_the_agents(seeded_db_path, snapshot):
    _run(seeded_db_path, snapshot)

    conn = sqlite3.connect(seeded_db_path)
    rows = conn.execute("SELECT actor, action FROM audit ORDER BY id").fetchall()
    conn.close()
    assert rows.count(("agent", "proposal.created")) == 2


# --- the suggested owner: only where evidenced -------------------------------------------------------------------------


def _snapshot_with_unowned_blocker():
    from pm.adapters.tracker import Sprint

    item = NormalizedItem(id="PM-040", title="Vendor sandbox access", status="blocked", raw_status="blocked", sprint_id="sprint-13",
                          assignee_id=None, created_at="2026-09-10T00:00:00+00:00", blocked_since="2026-09-15")
    return ProjectSnapshot(
        taken_at="2026-09-18T12:00:00+00:00", items=[item], commits=[], channel=ChannelSnapshot(channel_id="c", messages=[]),
        sprints=[Sprint(id="sprint-13", display_name="Sprint 13", start_date="2026-09-07", end_date="2026-09-20")],
        timezone="UTC",
    )


def test_a_blocker_nobody_owns_is_proposed_without_an_owner(seeded_db_path):
    snapshot = _snapshot_with_unowned_blocker()

    _run(seeded_db_path, snapshot)

    payload = _by_item(seeded_db_path)["PM-040"].payload
    assert payload["suggested_owner"] is None
    assert "no owner is evidenced" in payload["content"].lower()


def test_the_model_cannot_name_an_owner_the_evidence_does_not_support(seeded_db_path):
    snapshot = _snapshot_with_unowned_blocker()
    gaps = find_gaps(snapshot)
    gateway = ScriptedRiskGateway(gaps, rewrite=lambda lines, gap: [{**l, "text": l["text"] + " Priya should own this."} for l in lines], persistent=True)

    _run(seeded_db_path, snapshot, gateway)

    payload = _by_item(seeded_db_path)["PM-040"].payload
    assert "Priya" not in json.dumps(payload) and payload["suggested_owner"] is None


# --- the model is held to the facts -----------------------------------------------------------------------------------------

FORGERIES = {
    "a duration that is not the computed one": lambda text: text + " It has been blocked for 37 days.",
    "a duration hiding inside a date (9 is in 2026-09-14)": lambda text: text + " It has been blocked for 9 days.",
    "an invented consequence": lambda text: text + " This may delay the release.",
    "an invented owner": lambda text: text + " Priya should own this.",
    "the wrong person": lambda text: text.replace("blocked", "blocked, according to Noah Becker,"),
    "an invented date": lambda text: text + " Expected to clear on 2026-09-30.",
    "a date recombined from numbers the evidence has (4, 9, 2026)": lambda text: text + " It ends 2026-09-04.",
}


@pytest.mark.parametrize("persistent", [True, False], ids=["never-corrects", "corrects-on-retry"])
@pytest.mark.parametrize("name", sorted(FORGERIES))
def test_forged_prose_never_reaches_a_proposal(seeded_db_path, snapshot, name, persistent):
    forge = FORGERIES[name]
    gateway = ScriptedRiskGateway(
        find_gaps(snapshot), rewrite=lambda lines, gap: [{**l, "text": forge(l["text"])} for l in lines], persistent=persistent
    )

    _run(seeded_db_path, snapshot, gateway)

    proposals = _by_item(seeded_db_path)
    assert sorted(proposals) == ["PM-014", "PM-015"]  # the gap is real: still proposed, in templated words
    for proposal in proposals.values():
        text = json.dumps(proposal.payload)
        for forged in ("37 days", "blocked for 9 days", "delay the release", "Priya", "Noah", "2026-09-30", "2026-09-04"):
            assert forged not in text, forged
        if persistent:
            assert proposal.payload["prose_source"] == {"description": "template", "impact": "template"}


@pytest.mark.parametrize("what", ["wrong reference", "missing quote", "invented quote", "another blockers reference"])
def test_a_line_that_is_not_anchored_to_its_blocker_is_rejected(seeded_db_path, snapshot, what):
    def rewrite(lines, gap):
        forged = {
            "wrong reference": {"reference_id": "item:PM-999"},
            "missing quote": {"quote": None},
            "invented quote": {"quote": "words that appear nowhere in the evidence"},
            "another blockers reference": {"reference_id": "item:PM-015" if gap.item_id == "PM-014" else "item:PM-014"},
        }[what]
        return [{**line, **forged} for line in lines]

    gateway = ScriptedRiskGateway(find_gaps(snapshot), rewrite=rewrite, persistent=True)

    _run(seeded_db_path, snapshot, gateway)

    for proposal in _by_item(seeded_db_path).values():
        assert proposal.payload["prose_source"] == {"description": "template", "impact": "template"}
        assert proposal.original_model_output["dropped"]  # what grounding refused is on record


def test_a_model_that_corrects_itself_on_retry_is_used(seeded_db_path, snapshot):
    gateway = ScriptedRiskGateway(
        find_gaps(snapshot), rewrite=lambda lines, gap: [{**l, "text": l["text"] + " This may delay the release."} for l in lines]
    )

    _run(seeded_db_path, snapshot, gateway)

    for proposal in _by_item(seeded_db_path).values():
        assert proposal.payload["prose_source"] == {"description": "model", "impact": "model"}
        assert "delay" not in json.dumps(proposal.payload)


def test_a_model_that_only_writes_the_description_gets_a_templated_impact(seeded_db_path, snapshot):
    gateway = ScriptedRiskGateway(find_gaps(snapshot), rewrite=lambda lines, gap: [l for l in lines if l["kind"] == "description"],
                                  persistent=True)

    _run(seeded_db_path, snapshot, gateway)

    payload = _by_item(seeded_db_path)["PM-014"].payload
    assert payload["prose_source"] == {"description": "model", "impact": "template"} and payload["impact"]


def test_the_computed_numbers_are_the_proposals_whatever_the_model_says(seeded_db_path, snapshot):
    _run(seeded_db_path, snapshot)

    for item_id, (days, left) in {"PM-014": (4, 2), "PM-015": (1, 2)}.items():
        facts = _by_item(seeded_db_path)[item_id].payload["facts"]
        assert (facts["days_blocked"], facts["sprint_days_left"]) == (days, left)


# --- repeat runs and calls ----------------------------------------------------------------------------------------------


def test_running_again_does_not_propose_the_same_blocker_twice(seeded_db_path, snapshot):
    first = _run(seeded_db_path, snapshot)
    second = _run(seeded_db_path, snapshot)

    assert len(_proposals(seeded_db_path)) == 2
    assert all(r.created for r in first) and not any(r.created for r in second)
    assert [r.proposal_id for r in first] == [r.proposal_id for r in second]


def test_the_model_is_not_asked_again_about_a_blocker_already_proposed(seeded_db_path, snapshot):
    _run(seeded_db_path, snapshot)
    gateway = ScriptedRiskGateway(find_gaps(snapshot))

    _run(seeded_db_path, snapshot, gateway)

    assert gateway.calls == 0


def test_a_blocker_a_person_rejected_is_not_proposed_again(seeded_db_path, snapshot):
    _run(seeded_db_path, snapshot)
    pid = _by_item(seeded_db_path)["PM-014"].id
    reject(pid, approver_id="sharon.silva", reason="not a risk", policy=ApprovalPolicy(approver_ids=frozenset({"sharon.silva"})),
           db_path=seeded_db_path)

    again = _run(seeded_db_path, snapshot)

    assert ProposalStore(seeded_db_path).get(pid).status == REJECTED
    assert not any(r.created for r in again) and len(_proposals(seeded_db_path)) == 2


def test_a_new_blockage_of_the_same_item_is_a_new_proposal(seeded_db_path, snapshot):
    _run(seeded_db_path, snapshot)
    reject(_by_item(seeded_db_path)["PM-014"].id, approver_id="sharon.silva", reason="cleared",
           policy=ApprovalPolicy(approver_ids=frozenset({"sharon.silva"})), db_path=seeded_db_path)  # (PM-17) the old one is decided
    moved = snapshot.model_copy(update={"items": [
        i.model_copy(update={"blocked_since": "2026-09-18"}) if i.id == "PM-014" else i for i in snapshot.items
    ]})

    results = _run(seeded_db_path, moved)

    assert [r.item_id for r in results if r.created] == ["PM-014"] and len(_proposals(seeded_db_path)) == 3


def test_no_gaps_means_no_model_call_and_no_proposal(seeded_db_path):
    covered = _snapshot_with_unowned_blocker().model_copy(update={"items": []})
    gateway = ScriptedRiskGateway([])

    results = detect_and_propose(covered, gateway, db_path=seeded_db_path)

    assert results == [] and gateway.calls == 0 and _proposals(seeded_db_path) == []


def test_a_model_that_fails_outright_still_leaves_a_templated_proposal(seeded_db_path, snapshot):
    class Broken:
        def generate(self, prompt, **kwargs):
            return LLMResponse(text="this is not json", provider="f", model="f", prompt_tokens=0, completion_tokens=0,
                               latency_ms=0.0, cache_hit=False)

    results = detect_and_propose(snapshot, Broken(), db_path=seeded_db_path)

    assert [r.item_id for r in results] == ["PM-014", "PM-015"]
    for proposal in _by_item(seeded_db_path).values():
        assert proposal.payload["prose_source"] == {"description": "template", "impact": "template"}


def test_the_prompt_is_versioned_and_shows_only_the_evidence(seeded_db_path, snapshot):
    prompts = []

    class Spy(ScriptedRiskGateway):
        def generate(self, prompt, **kwargs):
            prompts.append(prompt)
            return super().generate(prompt, **kwargs)

    _run(seeded_db_path, snapshot, Spy(find_gaps(snapshot)))

    assert len(prompts) == 2 and "reference_id: item:PM-014" in prompts[0]
    assert "Search index blocked on staging DB migration" in prompts[0]
    assert "PM-023" not in prompts[0] and "PM-024" not in prompts[0]  # the model never sees an already-logged blocker
