"""PM-28: the risk log in plain language. Fixed templates over the rows; nothing the log does not say."""

from __future__ import annotations

from pm.adapters.risk_log import Risk
from pm.adapters.tracker import TrackerItem
from pm.api.risk_view import explain_risk, risk_summary


def risk(id_, severity, status="open", item="PM-024", title="Billing sync at risk") -> Risk:
    return Risk(id=id_, title=title, description="The nightly job may miss its SLA.", severity=severity, status=status,
                related_item_id=item, opened_at="2026-09-10")


def test_the_summary_counts_by_status_then_lists_the_most_severe_first():
    text = risk_summary([risk("RISK-003", "low"), risk("RISK-001", "high"), risk("RISK-002", "medium", status="mitigated"),
                         risk("RISK-004", "high")])

    lines = text.splitlines()
    assert lines[0] == "4 risks (1 mitigated, 3 open)."
    assert [line.split()[0] for line in lines[1:]] == ["RISK-001", "RISK-004", "RISK-002", "RISK-003"]  # high (by id), medium, low


def test_a_single_risk_is_not_pluralised():
    assert risk_summary([risk("RISK-001", "high")]).splitlines()[0] == "1 risk (1 open)."


def test_the_summary_says_what_was_asked_when_it_narrowed_the_log():
    assert risk_summary([risk("RISK-001", "high")], filters={"status": "open", "severity": None, "item": "PM-024"}).startswith(
        "1 risk for status open, item PM-024 (1 open).")


def test_an_empty_answer_says_there_are_no_risks_rather_than_staying_silent():
    assert risk_summary([], filters={"item": "PM-999"}) == "There are no risks in the log for item PM-999."
    assert risk_summary([]) == "There are no risks in the log."


def test_a_risk_with_no_item_is_listed_without_one():
    assert "RISK-009 (low, open): Vendor delay" in risk_summary([risk("RISK-009", "low", item=None, title="Vendor delay")])


def _item(blocked_since="2026-09-14") -> TrackerItem:
    return TrackerItem(id="PM-024", title="Billing sync nightly job", status="blocked", sprint_id="sprint-13", assignee_id="olivia.dupont",
                       created_at="2026-09-01", blocked_since=blocked_since)


def test_a_risk_is_explained_with_its_item_its_owner_and_how_long_it_has_been_blocked():
    text = explain_risk(risk("RISK-002", "high"), _item(), "Olivia Dupont", [])

    assert "RISK-002 is a high risk, open" in text and "PM-024 (Billing sync nightly job), which is blocked, assigned to Olivia Dupont" in text
    assert "blocked since 2026-09-14" in text and "Opened 2026-09-10." in text and "Waiting for a decision" not in text


def test_an_item_with_nobody_assigned_says_so_rather_than_guessing():
    assert "with nobody assigned" in explain_risk(risk("RISK-002", "high"), _item(), None, [])


def test_what_is_waiting_for_a_decision_is_named():
    text = explain_risk(risk("RISK-002", "high"), _item(), "Olivia Dupont", ["Proposed risk log entry for PM-024 (item:PM-024)"])

    assert "Waiting for a decision: Proposed risk log entry for PM-024 (item:PM-024)." in text


def test_a_risk_on_an_item_the_tracker_does_not_have_says_so():
    assert "PM-024, which is not in the tracker" in explain_risk(risk("RISK-002", "high"), None, None, [])
    assert "not tied to a tracker item" in explain_risk(risk("RISK-009", "low", item=None), None, None, [])
