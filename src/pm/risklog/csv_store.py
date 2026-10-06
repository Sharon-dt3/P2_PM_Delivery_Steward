"""The committed risk log: risk_log/risks.csv, the repo's system of record.

Plain CSV so a person can open it in Excel, Numbers or any editor and read it;
strictly validated on every read and write, so a bad edit is refused with every
problem named instead of being half applied; deterministic (rows sorted by id, fixed
column order, LF line endings) so a git diff shows exactly what changed; written
atomically so a crash can never leave a torn file.
"""

from __future__ import annotations

import csv
import io
import os
import re
from datetime import date
from pathlib import Path

from pm.adapters.risk_log import (
    DuplicateRiskError,
    Risk,
    RiskLogStore,
    RiskNotFoundError,
)

DEFAULT_CSV_PATH = Path(__file__).resolve().parents[3] / "risk_log" / "risks.csv"
COLUMNS = ["id", "title", "description", "severity", "status", "related_item_id", "opened_at"]
SEVERITIES = ("low", "medium", "high")
STATUSES = ("open", "mitigated", "closed")

_ID_RE = re.compile(r"^RISK-\d{3,}$")
_ITEM_RE = re.compile(r"^PM-\d+$")


class RiskLogDataError(ValueError):
    """The risk log is malformed or invalid. The message names every problem."""


def validate_risks(risks: list[Risk]) -> list[str]:
    """Every problem in the log, each starting with the risk's id. Empty if valid."""
    problems: list[str] = []
    seen: set[str] = set()
    for risk in risks:
        label = risk.id or "(no id)"
        if not _ID_RE.match(risk.id or ""):
            problems.append(f"{label}: id must look like RISK-001")
        if risk.id in seen:
            problems.append(f"{label}: id appears twice")
        seen.add(risk.id)
        if not (risk.title or "").strip():
            problems.append(f"{label}: title is empty")
        if risk.severity not in SEVERITIES:
            problems.append(f"{label}: severity {risk.severity!r} must be one of {', '.join(SEVERITIES)}")
        if risk.status not in STATUSES:
            problems.append(f"{label}: status {risk.status!r} must be one of {', '.join(STATUSES)}")
        if not _is_iso_date(risk.opened_at):
            problems.append(f"{label}: opened_at {risk.opened_at!r} must be a date like 2026-09-20")
        if risk.related_item_id is not None and not _ITEM_RE.match(risk.related_item_id):
            problems.append(f"{label}: related_item_id {risk.related_item_id!r} must look like PM-023 or be empty")
    return problems


def _is_iso_date(value: str | None) -> bool:
    try:
        return bool(value) and len(value) == 10 and date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def read_risks(path: str | Path = DEFAULT_CSV_PATH) -> list[Risk]:
    """The risks in `path`, sorted by id. Raises RiskLogDataError naming every
    problem; a bad file is never partly returned."""
    path = Path(path)
    if not path.exists():
        raise RiskLogDataError(f"risk log not found at {path}")
    text = path.read_bytes().decode("utf-8-sig")  # Excel saves UTF-8 CSV with a byte order mark
    reader = csv.DictReader(io.StringIO(text, newline=""))
    header = [h.strip() for h in (reader.fieldnames or [])]
    missing, extra = [c for c in COLUMNS if c not in header], [h for h in header if h not in COLUMNS]
    if missing or extra:
        raise RiskLogDataError(
            "the columns must be exactly " + ", ".join(COLUMNS)
            + (f"; missing: {', '.join(missing)}" if missing else "")
            + (f"; unexpected: {', '.join(extra)}" if extra else "")
        )
    risks: list[Risk] = []
    for row in reader:
        cells = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
        if not any(cells.values()):
            continue  # a blank line, as Excel leaves at the end of a file
        risks.append(
            Risk(**{c: cells[c] for c in COLUMNS if c != "related_item_id"}, related_item_id=cells["related_item_id"] or None)
        )
    problems = validate_risks(risks)
    if problems:
        raise RiskLogDataError("; ".join(problems))
    return sorted(risks, key=lambda r: r.id)


def write_risks(path: str | Path, risks: list[Risk]) -> None:
    """Validate, sort, and replace the file atomically. An invalid log changes nothing."""
    problems = validate_risks(list(risks))
    if problems:
        raise RiskLogDataError("; ".join(problems))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(COLUMNS)
    for risk in sorted(risks, key=lambda r: r.id):
        writer.writerow([risk.id, risk.title, risk.description, risk.severity, risk.status,
                         risk.related_item_id or "", risk.opened_at])
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(buffer.getvalue().encode("utf-8"))
    os.replace(temporary, path)


class CsvRiskLog(RiskLogStore):
    def __init__(self, path: str | Path = DEFAULT_CSV_PATH) -> None:
        self._path = Path(path)

    def list_risks(self) -> list[Risk]:
        return read_risks(self._path)

    def get_risk(self, risk_id: str) -> Risk:
        for risk in self.list_risks():
            if risk.id == risk_id:
                return risk
        raise RiskNotFoundError(risk_id)

    def create_risk(self, payload: Risk) -> Risk:
        risks = self.list_risks()
        if any(r.id == payload.id for r in risks):
            raise DuplicateRiskError(payload.id)
        write_risks(self._path, [*risks, payload])
        return payload

    def update_risk(self, risk_id: str, payload: Risk) -> Risk:
        if payload.id != risk_id:
            raise RiskLogDataError(f"{risk_id}: the id cannot be changed (got {payload.id})")
        risks = self.list_risks()
        if not any(r.id == risk_id for r in risks):
            raise RiskNotFoundError(risk_id)
        write_risks(self._path, [payload if r.id == risk_id else r for r in risks])
        return payload

    def replace_all(self, risks: list[Risk]) -> None:
        write_risks(self._path, list(risks))
