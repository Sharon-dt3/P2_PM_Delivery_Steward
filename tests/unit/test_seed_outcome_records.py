"""PM-01/02's two P1 OutcomeRecords -- one with allowlisted=False,
standing in for "one missing its scope/consent flag" (see build.py's own
comment on OUTCOME_RECORD_NOT_ALLOWLISTED for why that field is what the
master plan means by it). Round-trips through P1's own
write_outcome()/read_outcome() with zero new serialization code.
"""

from __future__ import annotations

from p1.contracts.outcome_record import read_outcome

from pm.seed.build import (
    CHANNEL_ID,
    OUTCOME_FIXTURES_DIR,
    OUTCOME_RECORD_ALLOWLISTED,
    OUTCOME_RECORD_NOT_ALLOWLISTED,
    build_outcome_fixtures,
)


def test_two_outcome_records_are_seeded_for_the_same_channel():
    assert OUTCOME_RECORD_ALLOWLISTED.channel_id == CHANNEL_ID
    assert OUTCOME_RECORD_NOT_ALLOWLISTED.channel_id == CHANNEL_ID
    assert OUTCOME_RECORD_ALLOWLISTED.date != OUTCOME_RECORD_NOT_ALLOWLISTED.date


def test_exactly_one_record_is_missing_its_scope_consent_flag():
    records = [OUTCOME_RECORD_ALLOWLISTED, OUTCOME_RECORD_NOT_ALLOWLISTED]
    not_allowlisted = [record for record in records if not record.allowlisted]
    assert len(not_allowlisted) == 1
    assert not_allowlisted[0] is OUTCOME_RECORD_NOT_ALLOWLISTED


def test_every_evidence_item_carries_a_message_id():
    """EvidenceItem.message_id is required by P1's own contract -- never
    a bare unsourced claim. Checked here too, not just trusted from the
    Pydantic model, since it's this seed's own job to satisfy that."""
    for record in (OUTCOME_RECORD_ALLOWLISTED, OUTCOME_RECORD_NOT_ALLOWLISTED):
        for section in (record.updates, record.blockers, record.decisions, record.questions):
            for evidence in section:
                assert evidence.message_id


def test_not_allowlisted_record_carries_no_fact_sections():
    record = OUTCOME_RECORD_NOT_ALLOWLISTED
    assert record.updates == []
    assert record.blockers == []
    assert record.decisions == []
    assert record.questions == []
    assert record.roster  # the roster/participation shell is still present


def test_outcome_fixtures_round_trip_through_p1s_own_read_outcome(tmp_path):
    paths = build_outcome_fixtures(output_dir=tmp_path)
    assert len(paths) == 2

    read_back_allowlisted = read_outcome(CHANNEL_ID, OUTCOME_RECORD_ALLOWLISTED.date, output_dir=tmp_path)
    assert read_back_allowlisted.allowlisted is True
    assert read_back_allowlisted.updates

    read_back_gated = read_outcome(CHANNEL_ID, OUTCOME_RECORD_NOT_ALLOWLISTED.date, output_dir=tmp_path)
    assert read_back_gated.allowlisted is False
    assert read_back_gated.updates == []


def test_committed_outcome_fixtures_match_what_the_module_would_write(tmp_path):
    """The JSON files committed under src/pm/seed/fixtures/outcomes/ are
    meant to be exactly what build_outcome_fixtures() produces -- this
    guards against the module and the committed fixture drifting apart
    (e.g. someone edits build.py's records without re-running
    scripts/seed.py)."""
    fresh_paths = build_outcome_fixtures(output_dir=tmp_path)
    for fresh_path in fresh_paths:
        rel = fresh_path.relative_to(tmp_path)
        committed_path = OUTCOME_FIXTURES_DIR / rel
        assert committed_path.exists(), f"committed fixture missing: {committed_path}"
        assert committed_path.read_text() == fresh_path.read_text()
