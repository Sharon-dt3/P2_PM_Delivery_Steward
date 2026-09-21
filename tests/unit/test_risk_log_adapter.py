from __future__ import annotations

import pytest

from pm.adapters.risk_log import (
    DuplicateRiskError,
    Risk,
    RiskLogMock,
    RiskLogStore,
    RiskNotFoundError,
)


@pytest.fixture()
def risk_log(seeded_db_path) -> RiskLogStore:
    return RiskLogMock(db_path=seeded_db_path)


def test_list_risks_returns_the_three_pre_existing_entries(risk_log):
    """PM-04's own edge case: three pre-existing entries."""
    assert len(risk_log.list_risks()) == 3


def test_two_risks_match_current_blockers(risk_log):
    """PM-04's own edge case: two matching current blockers."""
    risks = {risk.id: risk for risk in risk_log.list_risks()}
    assert risks["RISK-001"].related_item_id == "PM-023"
    assert risks["RISK-002"].related_item_id == "PM-024"
    assert risks["RISK-003"].related_item_id is None


def test_get_risk_raises_for_unknown_id(risk_log):
    with pytest.raises(RiskNotFoundError):
        risk_log.get_risk("RISK-999")


def test_create_risk_then_get_risk_round_trips(risk_log):
    payload = Risk(
        id="RISK-004", title="New risk", description="A new risk.",
        severity="low", status="open", opened_at="2026-09-18",
    )
    created = risk_log.create_risk(payload)
    assert created == payload
    assert risk_log.get_risk("RISK-004") == payload


def test_create_risk_raises_for_a_duplicate_id(risk_log):
    duplicate = Risk(
        id="RISK-001", title="dup", description="dup", severity="low", status="open", opened_at="2026-09-18",
    )
    with pytest.raises(DuplicateRiskError):
        risk_log.create_risk(duplicate)


def test_update_risk_persists_the_change(risk_log, seeded_db_path):
    original = risk_log.get_risk("RISK-002")
    updated_payload = original.model_copy(update={"status": "mitigated"})
    updated = risk_log.update_risk("RISK-002", updated_payload)
    assert updated.status == "mitigated"

    # a fresh instance, same db -- proves the update really persisted
    fresh = RiskLogMock(db_path=seeded_db_path)
    assert fresh.get_risk("RISK-002").status == "mitigated"


def test_update_risk_raises_for_unknown_id(risk_log):
    payload = Risk(id="RISK-999", title="x", description="x", severity="low", status="open", opened_at="2026-09-18")
    with pytest.raises(RiskNotFoundError):
        risk_log.update_risk("RISK-999", payload)
