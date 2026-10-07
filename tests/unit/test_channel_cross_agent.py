"""PM-26's acceptance test: P1's record file is consumed with no shared code beyond the schema.

Two independently built agents compose through a published, versioned file. Here P1's own code writes the record (the producer),
and P2 consumes the file in a process where nothing of P1's can be imported at all (the consumer): it reads the file, validates it
against the published schema, plans the two batches and creates the proposals. The copy of the schema P2 holds must also be the one
P1 publishes.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
P1_REPO = REPO.parent / "P3_Agents"
P1_SCHEMA = P1_REPO / "schema" / "outcome_record.v1.schema.json"
CHANNEL = "19:proj-gamma@thread.tacv2"

pytestmark = pytest.mark.skipif(not P1_SCHEMA.exists(), reason="P1's repo is not next to this one")


def _p1_writes_a_record(directory: Path) -> Path:
    """P1's own code produces the file, exactly as its daily job does."""
    from p1.contracts.outcome_record import (
        EvidenceItem,
        OutcomeRecord,
        ParticipationEntry,
        schema_version,
        write_outcome,
    )

    record = OutcomeRecord(
        schema_version=schema_version(), channel_id=CHANNEL, channel_display_name="Project Gamma", date=date(2026, 9, 18), allowlisted=True,
        roster=["wei.chen", "mateo.silva"], generated_at="2026-09-18T11:30:00+00:00",
        updates=[EvidenceItem(message_id="m1", text="PM-016 export bug is fixed and in review.", quote="export bug is fixed")],
        blockers=[
            EvidenceItem(message_id="m4", text="PM-014 is still blocked on the staging migration.", quote=None),
            EvidenceItem(message_id="m6", text="The adapter payload shape is undocumented.", quote=None),
        ],
        decisions=[], questions=[],
        participation=[ParticipationEntry(member_id="mateo.silva", state="no_message", evidence_message_ids=[])],
    )
    return write_outcome(record, output_dir=directory)


def test_the_schema_p2_reads_with_is_the_one_p1_publishes():
    assert (REPO / "contracts" / "outcome_record.v1.schema.json").read_bytes() == P1_SCHEMA.read_bytes()


def test_a_file_written_by_p1s_own_code_is_consumed_by_p2_with_p1_unimportable(tmp_path, seeded_db_path):
    path = _p1_writes_a_record(tmp_path / "outcomes")
    assert path.name == "2026-09-18.json"
    code = f"""
import json, sys
sys.modules['p1'] = None   # nothing of P1's can be imported from here on
sys.path[:0] = [{str(REPO / 'src')!r}, {str(REPO / 'packages' / 'spine' / 'src')!r}]
from pm.channel.batches import ItemFacts, RiskFacts, SprintFacts, TrackerView, consume
view = TrackerView(
    items={{'PM-016': ItemFacts('in_progress', 'mateo.silva'), 'PM-014': ItemFacts('blocked', 'olivia.dupree')}},
    risks=(RiskFacts('RISK-001', 'PM-023', 'open'),), sprints=(SprintFacts('sprint-13', '2026-09-07', '2026-09-20'),),
)
result = consume({str(path)!r}, view, db_path={str(seeded_db_path)!r})
assert not any(m == 'p1' or m.startswith('p1.') for m, v in sys.modules.items() if v is not None)
print(json.dumps({{
    'refused': result.refused is not None,
    'tracker': [(i['kind'], i.get('item_id'), i['reference']['ref']) for i in result.plan.tracker_items],
    'risk': [(i['related_item_id'], i['reference']['ref']) for i in result.plan.risk_items],
    'proposals': [result.tracker.proposal_id is not None, result.risk.proposal_id is not None],
}}))
"""

    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)

    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert out["refused"] is False and out["proposals"] == [True, True]
    assert out["tracker"] == [
        ["comment", "PM-016", f"teams:{CHANNEL}:m1"], ["comment", "PM-014", f"teams:{CHANNEL}:m4"], ["create", None, f"teams:{CHANNEL}:m6"],
    ]
    assert out["risk"] == [["PM-014", f"teams:{CHANNEL}:m4"], [None, f"teams:{CHANNEL}:m6"]]


def test_the_proposals_made_from_p1s_file_cite_the_channel_and_message_for_every_item(tmp_path, seeded_db_path):
    from spine.approval.proposals import ProposalStore

    from pm.adapters.risk_log import RiskLogMock
    from pm.adapters.tracker import TrackerMock
    from pm.channel.batches import TrackerView, consume

    path = _p1_writes_a_record(tmp_path / "outcomes")
    view = TrackerView.from_adapters(TrackerMock(db_path=seeded_db_path), RiskLogMock(db_path=seeded_db_path))

    result = consume(path, view, db_path=seeded_db_path)

    store = ProposalStore(seeded_db_path)
    for batch in (result.tracker, result.risk):
        proposal = store.get(batch.proposal_id)
        assert proposal.payload["channel_id"] == CHANNEL and proposal.payload["items"]
        for item in proposal.payload["items"]:
            assert item["reference"]["ref"].startswith(f"teams:{CHANNEL}:m") and item["reference"]["channel_display_name"] == "Project Gamma"
        assert set(proposal.source_refs) == {i["reference"]["ref"] for i in proposal.payload["items"]}


def test_a_record_p1_wrote_as_not_cleared_is_refused_by_p2(tmp_path, seeded_db_path):
    from p1.contracts.outcome_record import OutcomeRecord, write_outcome

    from pm.channel.batches import TrackerView, consume

    record = OutcomeRecord(
        schema_version="1.0", channel_id=CHANNEL, channel_display_name="Project Gamma", date=date(2026, 9, 18), allowlisted=False,
        roster=[], generated_at="2026-09-18T11:30:00+00:00",
    )
    path = write_outcome(record, output_dir=tmp_path / "outcomes")

    result = consume(path, TrackerView(items={}), db_path=seeded_db_path)

    assert result.refused.code == "not_allowlisted" and result.tracker is None and result.risk is None


def test_the_live_record_p1_has_written_is_read_by_p2_when_there_is_one():
    live = sorted((P1_REPO / "outcomes").glob("*/*.json"))
    if not live:
        pytest.skip("P1 has not written a record in this checkout")
    from pm.channel.record import RecordRefused, load_record

    try:
        record = load_record(live[-1])
    except RecordRefused as refusal:
        assert refusal.code == "not_allowlisted"  # a real file is either read, or refused only because P1 was not cleared to pass it on
    else:
        assert record.channel_id and record.date and record.allowlisted is True
