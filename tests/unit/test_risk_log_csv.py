"""PM-15: the committed CSV risk log -- the repo's system of record.

A person can open it in Excel, Numbers or any editor and read it; it is strictly
validated on every read and write, so a bad edit is refused with every problem
named, never half-applied; and its bytes are deterministic (sorted, fixed column
order), so a git diff shows exactly what changed.
"""

from __future__ import annotations

import csv

import pytest

from pm.adapters.risk_log import DuplicateRiskError, Risk, RiskNotFoundError
from pm.risklog.csv_store import (
    COLUMNS,
    CsvRiskLog,
    RiskLogDataError,
    read_risks,
    validate_risks,
    write_risks,
)
from pm.seed.build import SEED_RISK_LOG_PATH

SEEDED = [
    Risk(id="RISK-001", title="Auth flow token refresh intermittent failures in staging",
         description="Token refresh calls are failing intermittently under load in the staging environment; root cause not yet confirmed.",
         severity="medium", status="open", related_item_id="PM-023", opened_at="2026-09-10"),
    Risk(id="RISK-002", title="Billing sync nightly job at risk of missing SLA",
         description="The nightly billing sync job has been running past its committed SLA window twice this sprint; vendor-side latency suspected.",
         severity="high", status="open", related_item_id="PM-024", opened_at="2026-09-11"),
    Risk(id="RISK-003", title="Third-party billing API rate limits may throttle nightly sync during peak season",
         description="Flagged during Sprint 12 planning; mitigated by moving the sync off-peak. No currently-open blocker traces back to this.",
         severity="low", status="mitigated", related_item_id=None, opened_at="2026-08-20"),
]


def _risk(**changes):
    values = {"id": "RISK-010", "title": "A new risk", "description": "Because.", "severity": "low",
              "status": "open", "related_item_id": None, "opened_at": "2026-09-20"}
    return Risk(**{**values, **changes})


# --- the committed file ------------------------------------------------------------------------------------


def test_the_frozen_seed_holds_exactly_the_three_seeded_risks():
    assert read_risks(SEED_RISK_LOG_PATH) == SEEDED


def test_the_committed_csv_is_human_readable_plain_text():
    text = SEED_RISK_LOG_PATH.read_text(encoding="utf-8")

    assert text.splitlines()[0] == ",".join(COLUMNS) == "id,title,description,severity,status,related_item_id,opened_at"
    assert "Billing sync nightly job at risk of missing SLA" in text and "RISK-003" in text
    rows = list(csv.DictReader(text.splitlines()))
    assert [r["id"] for r in rows] == ["RISK-001", "RISK-002", "RISK-003"]


# --- reading and writing ----------------------------------------------------------------------------------


def test_a_log_round_trips_exactly(tmp_path):
    path = tmp_path / "risks.csv"

    write_risks(path, SEEDED)

    assert read_risks(path) == SEEDED


def test_the_file_is_deterministic_sorted_by_id_with_a_fixed_column_order(tmp_path):
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"

    write_risks(a, [SEEDED[2], SEEDED[0], SEEDED[1]])
    write_risks(b, SEEDED)

    assert a.read_bytes() == b.read_bytes()
    assert [r["id"] for r in csv.DictReader(a.read_text().splitlines())] == ["RISK-001", "RISK-002", "RISK-003"]


def test_commas_quotes_and_newlines_in_text_survive(tmp_path):
    path = tmp_path / "risks.csv"
    tricky = _risk(title='Vendor says "ready by Friday", maybe', description="Line one.\nLine two, with a comma.")

    write_risks(path, [tricky])

    assert read_risks(path) == [tricky]


def test_non_ascii_text_survives(tmp_path):
    path = tmp_path / "risks.csv"

    write_risks(path, [_risk(title="Café déploiement — résumé", description="naïve")])

    assert read_risks(path)[0].title == "Café déploiement — résumé"


def test_a_file_saved_by_excel_with_a_byte_order_mark_and_blank_lines_is_read(tmp_path):
    path = tmp_path / "risks.csv"
    text = ",".join(COLUMNS) + "\nRISK-010,A risk,Why,low,open,,2026-09-20\n\n\n"
    path.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))

    assert read_risks(path) == [_risk(title="A risk", description="Why")]


def test_columns_may_be_in_any_order_but_must_all_be_there(tmp_path):
    path = tmp_path / "risks.csv"
    path.write_text("opened_at,id,status,severity,title,description,related_item_id\n2026-09-20,RISK-010,open,low,T,D,\n")

    assert read_risks(path) == [_risk(title="T", description="D")]

    path.write_text("id,title,severity\nRISK-010,T,low\n")
    with pytest.raises(RiskLogDataError, match="columns"):
        read_risks(path)


def test_an_extra_column_is_refused_not_silently_dropped(tmp_path):
    path = tmp_path / "risks.csv"
    path.write_text(",".join(COLUMNS) + ",assignee\nRISK-010,T,D,low,open,,2026-09-20,sam\n")

    with pytest.raises(RiskLogDataError, match="assignee"):
        read_risks(path)


def test_a_missing_file_is_a_clean_error(tmp_path):
    with pytest.raises(RiskLogDataError, match="not found"):
        read_risks(tmp_path / "nope.csv")


# --- validation ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "changes, fragment",
    [
        ({"severity": "urgent"}, "severity"),
        ({"status": "done"}, "status"),
        ({"id": "R-1"}, "id"),
        ({"title": "  "}, "title"),
        ({"opened_at": "10 Sept"}, "opened_at"),
        ({"opened_at": "2026-02-31"}, "opened_at"),
        ({"related_item_id": "TICKET-9"}, "related_item_id"),
    ],
)
def test_bad_values_are_named(changes, fragment):
    problems = validate_risks([_risk(**changes)])

    assert problems and any(fragment in p for p in problems), problems


def test_every_problem_is_reported_at_once_not_just_the_first():
    problems = validate_risks([_risk(severity="urgent", status="done", title="")])

    assert len(problems) >= 3


def test_duplicate_ids_are_refused():
    assert any("RISK-010" in p and "twice" in p for p in validate_risks([_risk(), _risk(title="again")]))


def test_a_bad_file_is_refused_with_every_problem_and_nothing_is_returned(tmp_path):
    path = tmp_path / "risks.csv"
    path.write_text(",".join(COLUMNS) + "\nRISK-010,T,D,urgent,open,,2026-09-20\nRISK-011,T,D,low,done,,2026-09-20\n")

    with pytest.raises(RiskLogDataError) as excinfo:
        read_risks(path)

    assert "urgent" in str(excinfo.value) and "done" in str(excinfo.value)


def test_writing_an_invalid_log_changes_nothing(tmp_path):
    path = tmp_path / "risks.csv"
    write_risks(path, SEEDED)
    before = path.read_bytes()

    with pytest.raises(RiskLogDataError):
        write_risks(path, [*SEEDED, _risk(severity="urgent")])

    assert path.read_bytes() == before  # atomic: the old file is intact
    assert not list(tmp_path.glob("*.tmp"))  # and no stray temp file is left


def test_related_item_is_optional_and_empty_means_none(tmp_path):
    path = tmp_path / "risks.csv"

    write_risks(path, [_risk(related_item_id=None)])

    assert read_risks(path)[0].related_item_id is None


# --- the store interface ---------------------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path):
    path = tmp_path / "risks.csv"
    write_risks(path, SEEDED)
    return CsvRiskLog(path)


def test_list_and_get(store):
    assert store.list_risks() == SEEDED
    assert store.get_risk("RISK-002").severity == "high"
    with pytest.raises(RiskNotFoundError):
        store.get_risk("RISK-999")


def test_create_adds_a_row_and_refuses_a_duplicate(store):
    store.create_risk(_risk())

    assert [r.id for r in store.list_risks()] == ["RISK-001", "RISK-002", "RISK-003", "RISK-010"]
    with pytest.raises(DuplicateRiskError):
        store.create_risk(_risk(title="again"))


def test_update_replaces_a_row_and_refuses_an_unknown_one(store):
    store.update_risk("RISK-001", SEEDED[0].model_copy(update={"status": "mitigated"}))

    assert store.get_risk("RISK-001").status == "mitigated"
    with pytest.raises(RiskNotFoundError):
        store.update_risk("RISK-999", _risk(id="RISK-999"))


def test_update_cannot_change_the_id(store):
    with pytest.raises(RiskLogDataError, match="id"):
        store.update_risk("RISK-001", SEEDED[0].model_copy(update={"id": "RISK-777"}))


def test_a_write_through_the_store_validates(store):
    with pytest.raises(RiskLogDataError):
        store.create_risk(_risk(id="RISK-011", severity="urgent"))

    assert len(store.list_risks()) == 3


def test_replace_all_swaps_the_whole_log(store):
    store.replace_all([SEEDED[1]])

    assert store.list_risks() == [SEEDED[1]]


# --- the optional owner column ------------------------------------------------------------------------------------


def test_a_log_with_no_owners_is_written_exactly_as_before_the_column_existed(tmp_path):
    path = tmp_path / "risks.csv"

    write_risks(path, SEEDED)

    assert path.read_text(encoding="utf-8") == SEED_RISK_LOG_PATH.read_text(encoding="utf-8")  # byte for byte: no empty owner column appears


def test_an_owner_is_written_in_its_own_column_and_read_back(tmp_path):
    path = tmp_path / "risks.csv"
    with_owner = [*SEEDED, _risk(owner="Olivia Dupree (olivia.dupree)")]

    write_risks(path, with_owner)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == ",".join(COLUMNS) + ",owner" and lines[-1].endswith(",Olivia Dupree (olivia.dupree)")
    assert read_risks(path) == with_owner and read_risks(path)[0].owner is None


def test_a_file_the_lead_saved_with_an_owner_column_and_blank_owners_reads_with_none(tmp_path):
    path = tmp_path / "risks.csv"
    path.write_text(",".join(COLUMNS) + ",owner\nRISK-010,T,D,low,open,,2026-09-20,\nRISK-011,T,D,low,open,,2026-09-20,Sam\n")

    first, second = read_risks(path)

    assert first.owner is None and second.owner == "Sam"
