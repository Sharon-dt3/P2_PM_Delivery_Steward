"""PM-27 on the commitment feed (PM-24): a record whose scope/consent flag is not explicitly true adds nothing.

The feed used to read records through P1's own pydantic model, which turns "true", "yes" and 1 into True: a record with a sloppy
flag passed as cleared. It now reads through the same strict reader as PM-26 (pm.channel.record) and imports nothing of P1's.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from pm.channel.record import RecordRefused
from pm.commitments.outcomes import (
    ADDED,
    CONSENT_WITHHELD,
    REFUSED,
    MessageInfo,
    ingest_outcome_record,
    ingest_recent_outcomes,
    ingest_record_file,
)
from pm.commitments.store import CommitmentTracker

CHANNEL = "19:proj-gamma@thread.tacv2"
INFO = {"m1": MessageInfo("mateo.silva", "2026-09-18T09:00:00+00:00")}
ROSTER = {"mateo.silva"}


def record_dict(**overrides) -> dict:
    data = {
        "schema_version": "1.0", "channel_id": CHANNEL, "channel_display_name": "Project Gamma", "date": "2026-09-18", "allowlisted": True,
        "roster": [], "generated_at": "2026-09-18T11:30:00+00:00", "participation": [], "decisions": [], "questions": [], "blockers": [],
        "updates": [{"message_id": "m1", "text": "I'll have the reporting dashboard export bug fixed by Friday.", "quote": None}],
    }
    data.update(overrides)
    return data


def _file(tmp_path, data) -> Path:
    path = tmp_path / "2026-09-18.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _commitments(db) -> int:
    return sqlite3.connect(db).execute("SELECT COUNT(*) FROM commitments").fetchone()[0]


def _refusals(db) -> list[dict]:
    rows = sqlite3.connect(db).execute("SELECT details FROM audit WHERE action = 'record.refused'").fetchall()
    return [json.loads(r[0]) for r in rows]


def test_a_cleared_record_file_adds_its_commitment(tmp_path, seeded_db_path):
    before = _commitments(seeded_db_path)

    results = ingest_record_file(_file(tmp_path, record_dict()), tracker=CommitmentTracker(seeded_db_path), message_info=INFO.get, roster=ROSTER)

    assert [r.status for r in results] == [ADDED] and _commitments(seeded_db_path) == before + 1 and _refusals(seeded_db_path) == []


@pytest.mark.parametrize("flag", [False, "true", "yes", "1", 1, None, [True], "missing"], ids=lambda f: f"flag={f!r}")
def test_a_flag_that_is_not_explicitly_true_adds_nothing_and_is_logged_once(tmp_path, seeded_db_path, flag):
    data = {k: v for k, v in record_dict().items() if k != "allowlisted"} if flag == "missing" else record_dict(allowlisted=flag)
    before = _commitments(seeded_db_path)

    results = ingest_record_file(_file(tmp_path, data), tracker=CommitmentTracker(seeded_db_path), message_info=INFO.get, roster=ROSTER)

    assert [r.status for r in results] == [CONSENT_WITHHELD] and _commitments(seeded_db_path) == before
    (logged,) = _refusals(seeded_db_path)
    assert logged["code"] == "not_allowlisted" and logged["consumer"] == "commitment_feed" and logged["reason"]


@pytest.mark.parametrize("data, code", [("{ not json", "not_json"), (record_dict(schema_version="2.0"), "unsupported_version"),
                                        (record_dict(updates=[{"text": "no id"}]), "schema_invalid")])
def test_a_file_that_is_not_the_published_contract_adds_nothing_and_is_logged_once(tmp_path, seeded_db_path, data, code):
    before = _commitments(seeded_db_path)

    results = ingest_record_file(_file(tmp_path, data) if not isinstance(data, str) else _text(tmp_path, data), tracker=CommitmentTracker(seeded_db_path),
                                 message_info=INFO.get, roster=ROSTER)

    assert [r.status for r in results] == [REFUSED] and _commitments(seeded_db_path) == before
    assert [r["code"] for r in _refusals(seeded_db_path)] == [code]


def _text(tmp_path, text) -> Path:
    path = tmp_path / "2026-09-18.json"
    path.write_text(text, encoding="utf-8")
    return path


def test_the_recent_records_folder_is_read_through_the_same_gate(tmp_path, seeded_db_path):
    folder = tmp_path / "outcomes" / "19_proj-gamma_thread.tacv2"  # the contract's layout for this channel id
    folder.mkdir(parents=True)
    (folder / "2026-09-18.json").write_text(json.dumps(record_dict(allowlisted="yes")), encoding="utf-8")
    before = _commitments(seeded_db_path)

    results = ingest_recent_outcomes(channel_id=CHANNEL, today=date(2026, 9, 18), days_back=0, output_dir=tmp_path / "outcomes",
                                     tracker=CommitmentTracker(seeded_db_path), message_info=INFO.get, roster=ROSTER)

    assert [r.status for r in results] == [CONSENT_WITHHELD] and _commitments(seeded_db_path) == before
    assert [r["consumer"] for r in _refusals(seeded_db_path)] == ["commitment_feed"]


def test_an_object_handed_in_directly_with_a_truthy_flag_that_is_not_true_is_still_withheld(seeded_db_path):
    """Defence in depth: whatever reads the file, the ingest itself will not take anything but an explicit True."""
    class Loose:
        channel_id, date, allowlisted = CHANNEL, "2026-09-18", "yes"
        updates = decisions = ()

    results = ingest_outcome_record(Loose(), tracker=CommitmentTracker(seeded_db_path), message_info=INFO.get, roster=ROSTER)

    assert [r.status for r in results] == [CONSENT_WITHHELD]


def test_the_feed_no_longer_imports_anything_of_p1():
    source = (Path(__file__).resolve().parents[2] / "src" / "pm" / "commitments" / "outcomes.py").read_text()

    assert "from p1" not in source and "import p1" not in source


def test_a_refusal_raised_by_the_loader_is_a_value_error():
    assert issubclass(RecordRefused, ValueError)
