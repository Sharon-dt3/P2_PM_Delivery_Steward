"""Reading P1's channel outcome record (CHN-26) with nothing but the JSON file and its published schema.

This is the P2 side of the cross-agent contract. Two agents built independently compose through a published, versioned
file: P1 writes `outcomes/<channel>/<date>.json` and publishes `outcome_record.v1.schema.json`; this module reads the
file and checks it against a copy of that schema (contracts/outcome_record.v1.schema.json). It imports nothing of P1's:
no `p1.*`, no model class, no helper. `tests/unit/test_channel_record.py` runs it with `p1` made un-importable, and
`tests/unit/test_channel_cross_agent.py` fails if the copy of the schema and P1's published one ever differ.

What it refuses, each with a code and a reason in words (a RecordRefused; nothing else is ever raised for a bad file):

  unreadable           the file cannot be read
  not_json             it is not JSON
  schema_invalid       it is JSON but not the published schema (a missing or mistyped field, a bad date, ...)
  unsupported_version  a major version this reader does not know (it reads 1.x, and tolerates fields added in a later 1.x)
  not_allowlisted      the scope/consent flag is not exactly the JSON value true: P1 was not cleared to pass this
                       channel's content on, so nothing is taken from it (PM-27's rule, applied at the door so nothing
                       downstream can ever see such content)

The flag is checked after the schema, and the schema types it as a boolean, so "true", 1 and null never pass as true.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import jsonschema

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "contracts" / "outcome_record.v1.schema.json"
SUPPORTED_MAJOR = 1
SECTIONS = (("updates", "update"), ("blockers", "blocker"), ("decisions", "decision"), ("questions", "question"))

UNREADABLE, NOT_JSON, SCHEMA_INVALID, UNSUPPORTED_VERSION, NOT_ALLOWLISTED = (
    "unreadable", "not_json", "schema_invalid", "unsupported_version", "not_allowlisted",
)


class RecordRefused(Exception):
    """The record was not used. `code` is machine-readable, `reason` is for a person (and for the audit log)."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(f"{code}: {reason}")
        self.code, self.reason = code, reason


@dataclass(frozen=True)
class Evidence:
    """One grounded line of the record: the message it came from, the one-line prose, and a verbatim quote if there was one."""

    message_id: str
    text: str
    quote: str | None
    section: str  # update | blocker | decision | question


@dataclass(frozen=True)
class Participation:
    member_id: str
    state: str
    evidence_message_ids: tuple[str, ...]


@dataclass(frozen=True)
class ChannelRecord:
    schema_version: str
    channel_id: str
    channel_display_name: str
    date: str
    allowlisted: bool
    roster: tuple[str, ...]
    generated_at: str
    updates: tuple[Evidence, ...]
    blockers: tuple[Evidence, ...]
    decisions: tuple[Evidence, ...]
    questions: tuple[Evidence, ...]
    participation: tuple[Participation, ...]

    def evidence(self) -> list[Evidence]:
        """Every line, in the order updates, blockers, decisions, questions."""
        return [*self.updates, *self.blockers, *self.decisions, *self.questions]


_validator: jsonschema.Draft202012Validator | None = None


def _schema_validator() -> jsonschema.Draft202012Validator:
    global _validator
    if _validator is None:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        _validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER)
    return _validator


def _problem(error: jsonschema.ValidationError) -> str:
    where = ".".join(str(p) for p in error.absolute_path) or "the record"
    return f"{where}: {error.message[:160]}"


def _major(version: str) -> int | None:
    match = re.fullmatch(r"(\d+)(?:\.\d+)*", version.strip())
    return int(match.group(1)) if match else None


def load_record(path: str | Path) -> ChannelRecord:
    """Read, validate and refuse (RecordRefused) or return the record. Reads the one file; touches nothing else."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RecordRefused(UNREADABLE, f"{path.name} could not be read ({type(exc).__name__})") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise RecordRefused(NOT_JSON, f"{path.name} is not valid JSON ({exc})") from exc

    errors = sorted(_schema_validator().iter_errors(data), key=lambda e: list(e.absolute_path))
    if errors:
        more = f" (and {len(errors) - 1} more)" if len(errors) > 1 else ""
        raise RecordRefused(SCHEMA_INVALID, f"{path.name} does not match the published schema: {_problem(errors[0])}{more}")

    version = data["schema_version"]
    if _major(version) != SUPPORTED_MAJOR:
        raise RecordRefused(UNSUPPORTED_VERSION, f"schema_version {version!r}: this reader knows version {SUPPORTED_MAJOR}.x only")
    if data["allowlisted"] is not True:
        raise RecordRefused(
            NOT_ALLOWLISTED,
            f"{data['channel_id']} {data['date']}: the scope/consent flag is not true, so P1 was not cleared to pass this channel's content on",
        )

    def lines(key: str, section: str) -> tuple[Evidence, ...]:
        return tuple(Evidence(i["message_id"], i["text"], i.get("quote"), section) for i in data.get(key, []))

    return ChannelRecord(
        schema_version=version, channel_id=data["channel_id"], channel_display_name=data["channel_display_name"], date=data["date"],
        allowlisted=True, roster=tuple(data["roster"]), generated_at=data["generated_at"],
        updates=lines("updates", "update"), blockers=lines("blockers", "blocker"),
        decisions=lines("decisions", "decision"), questions=lines("questions", "question"),
        participation=tuple(
            Participation(p["member_id"], p["state"], tuple(p.get("evidence_message_ids", []))) for p in data.get("participation", [])
        ),
    )
