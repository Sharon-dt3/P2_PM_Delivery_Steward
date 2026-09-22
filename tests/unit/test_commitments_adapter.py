from __future__ import annotations

import pytest

from pm.adapters.commitments import Commitment, CommitmentNotFoundError, CommitmentsMock


@pytest.fixture()
def commitments_store(seeded_db_path) -> CommitmentsMock:
    return CommitmentsMock(db_path=seeded_db_path)


def test_list_commitments_returns_every_seeded_commitment(commitments_store):
    commitments = commitments_store.list_commitments()
    assert len(commitments) == 8
    assert all(isinstance(commitment, Commitment) for commitment in commitments)


def test_list_commitments_filters_by_member(commitments_store):
    """wei.chen made two seeded commitments (PM-028, PM-023)."""
    commitments = commitments_store.list_commitments(member_id="wei.chen")
    assert {commitment.item_id for commitment in commitments} == {"PM-028", "PM-023"}


def test_list_commitments_surfaces_the_relative_due_date_edge_case(commitments_store):
    """PM-03 difficulty 9: a commitment with no due_date_iso, only a
    relative phrase."""
    commitments = commitments_store.list_commitments(member_id="mateo.silva")
    assert len(commitments) == 1
    assert commitments[0].due_date_iso is None
    assert commitments[0].due_date_text == "end of week"


def test_get_commitment_returns_the_matching_row(commitments_store):
    commitments = commitments_store.list_commitments(member_id="mateo.silva")
    commitment = commitments_store.get_commitment(commitments[0].id)
    assert commitment == commitments[0]


def test_get_commitment_raises_for_unknown_id(commitments_store):
    with pytest.raises(CommitmentNotFoundError):
        commitments_store.get_commitment(999999)
