"""PM-26: reading P1's channel outcome record with nothing but the JSON and the published schema.

The reader (pm.channel.record) imports no part of P1. It validates the file against the published JSON Schema, then
refuses, with a reason, anything it should not use: a file it cannot read, something that is not the schema, a major
version it does not know, and any record whose scope/consent flag is not explicitly true (PM-27's rule, applied here
so nothing in the proposal path can ever see content P1 was not cleared to pass on).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from pm.channel.record import ChannelRecord, RecordRefused, load_record

REPO = Path(__file__).resolve().parents[2]
CHANNEL = "19:proj-gamma@thread.tacv2"


def record_dict(**overrides) -> dict:
    record = {
        "schema_version": "1.0", "channel_id": CHANNEL, "channel_display_name": "Project Gamma", "date": "2026-09-18",
        "allowlisted": True, "roster": ["wei.chen", "mateo.silva"], "generated_at": "2026-09-18T11:30:00+00:00",
        "updates": [{"message_id": "m1", "text": "PM-016 export bug is fixed and in review.", "quote": "export bug is fixed"}],
        "blockers": [{"message_id": "m4", "text": "PM-014 is still blocked on the staging migration.", "quote": None}],
        "decisions": [], "questions": [],
        "participation": [{"member_id": "mateo.silva", "state": "no_message", "evidence_message_ids": []}],
    }
    record.update(overrides)
    return record


def write(tmp_path: Path, data, name: str = "2026-09-18.json") -> Path:
    path = tmp_path / name
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


def test_a_valid_record_is_read_into_plain_objects(tmp_path):
    record = load_record(write(tmp_path, record_dict()))

    assert isinstance(record, ChannelRecord)
    assert (record.channel_id, record.date, record.allowlisted, record.schema_version) == (CHANNEL, "2026-09-18", True, "1.0")
    assert [(e.message_id, e.section) for e in record.updates] == [("m1", "update")]
    assert record.blockers[0].quote is None and record.updates[0].quote == "export bug is fixed"
    assert [(p.member_id, p.state) for p in record.participation] == [("mateo.silva", "no_message")]


def test_a_later_minor_version_with_extra_fields_is_still_read(tmp_path):
    data = record_dict(schema_version="1.3", something_new={"a": 1})
    data["updates"][0]["confidence"] = 0.9

    assert load_record(write(tmp_path, data)).schema_version == "1.3"


@pytest.mark.parametrize("data, code", [
    (None, "unreadable"),  # the file does not exist
    ("{ not json", "not_json"),
    ("[1, 2, 3]", "schema_invalid"),
    (record_dict(channel_id=None), "schema_invalid"),
    ({k: v for k, v in record_dict().items() if k != "allowlisted"}, "not_allowlisted"),  # the flag must be there at all
    (record_dict(allowlisted="true"), "not_allowlisted"),  # a string is not a boolean
    (record_dict(allowlisted="yes"), "not_allowlisted"),
    (record_dict(allowlisted=1), "not_allowlisted"),
    (record_dict(allowlisted=None), "not_allowlisted"),
    (record_dict(allowlisted=[True]), "not_allowlisted"),
    (record_dict(allowlisted={"cleared": True}), "not_allowlisted"),
    (record_dict(allowlisted=False, schema_version="2.0"), "not_allowlisted"),  # the flag is looked at first
    ({k: v for k, v in record_dict(date="18 September").items() if k != "allowlisted"}, "not_allowlisted"),  # even in a file that is also malformed
    (record_dict(date="18 September"), "schema_invalid"),
    (record_dict(updates=[{"text": "no message id"}]), "schema_invalid"),
    (record_dict(roster="wei.chen"), "schema_invalid"),
    (record_dict(schema_version="2.0"), "unsupported_version"),
    (record_dict(schema_version="one"), "unsupported_version"),
    (record_dict(allowlisted=False), "not_allowlisted"),
], ids=["missing-file", "not-json", "not-an-object", "null-channel", "flag-missing", "flag-string", "flag-yes", "flag-one", "flag-null",
        "flag-list", "flag-object", "flag-false-and-major-2", "flag-missing-and-malformed", "bad-date",
        "item-without-id", "roster-not-a-list", "major-2", "version-not-a-number", "not-allowlisted"])
def test_anything_it_should_not_use_is_refused_with_a_code_and_a_reason(tmp_path, data, code):
    path = tmp_path / "nope.json" if data is None else write(tmp_path, data)

    with pytest.raises(RecordRefused) as refused:
        load_record(path)

    assert refused.value.code == code and refused.value.reason


def test_only_an_explicit_true_passes_the_consent_flag(tmp_path):
    assert load_record(write(tmp_path, record_dict(allowlisted=True))).allowlisted is True
    with pytest.raises(RecordRefused) as refused:
        load_record(write(tmp_path, record_dict(allowlisted=False), "off.json"))
    assert refused.value.code == "not_allowlisted" and "not cleared" in refused.value.reason


@pytest.mark.parametrize("flag, words", [
    (False, "is false"), ("yes", "'yes'"), (1, "1"), (None, "None"), ("missing", "missing"),
], ids=["false", "yes", "one", "null", "missing"])
def test_a_flag_refusal_says_which_flag_problem_it_was(tmp_path, flag, words):
    data = {k: v for k, v in record_dict().items() if k != "allowlisted"} if flag == "missing" else record_dict(allowlisted=flag)

    with pytest.raises(RecordRefused) as refused:
        load_record(write(tmp_path, data))

    assert refused.value.code == "not_allowlisted" and words in refused.value.reason and "not cleared" in refused.value.reason


def test_a_refusal_is_a_value_error_so_a_caller_that_catches_bad_data_catches_it(tmp_path):
    with pytest.raises(ValueError):
        load_record(write(tmp_path, record_dict(allowlisted=False)))


def test_a_refusal_names_what_was_wrong_in_words(tmp_path):
    with pytest.raises(RecordRefused) as refused:
        load_record(write(tmp_path, record_dict(date="18 September")))

    assert "date" in refused.value.reason


def test_the_reader_works_with_p1_made_unimportable(tmp_path):
    """No shared code beyond the schema: with `p1` blocked in sys.modules, the reader still reads a record."""
    path = write(tmp_path, record_dict())
    code = (
        "import sys; sys.modules['p1'] = None\n"
        f"sys.path[:0] = [{str(REPO / 'src')!r}, {str(REPO / 'packages' / 'spine' / 'src')!r}]\n"
        "from pm.channel.record import load_record\n"
        f"r = load_record({str(path)!r})\n"
        "print(r.channel_id, r.date, len(r.updates), len(r.blockers))\n"
        "assert not any(m == 'p1' or m.startswith('p1.') for m, v in sys.modules.items() if v is not None)\n"
    )

    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)

    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == [CHANNEL, "2026-09-18", "1", "1"]
