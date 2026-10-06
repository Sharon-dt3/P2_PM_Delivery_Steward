"""Keep the three risk logs in sync: the committed CSV (system of record), the
lead-facing editable table, and the runtime copy in the database the brief reads.

The sync is three-way against the last state both sides agreed on (a fingerprint
kept in a small baseline file), so it knows WHICH side moved:

  repo == lead store                  in sync
  only the lead's table changed       pull it into the CSV (and the runtime copy)
  only the repo CSV changed           push it to the lead's table
  both changed                        CONFLICT: say what differs, change nothing
  never synced, and they differ       CONFLICT, unless the lead's table is empty
  the lead's table is suddenly empty  CONFLICT: that is not an instruction to wipe the record

Nothing is ever applied if it is invalid: a bad edit in the lead's table (an unknown
severity, a risk pointing at a tracker item that does not exist) is refused with every
problem named and the repo copy is untouched; an unreachable lead store changes
nothing, and the repo copy keeps working offline. `push` and `pull` force a direction
deliberately; every operation has a dry run.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from pm.adapters.risk_log import Risk
from pm.risklog.csv_store import CsvRiskLog, RiskLogDataError, validate_risks
from pm.risklog.remote import RemoteRiskLog, RemoteUnavailableError
from pm.storage.db import get_connection

IN_SYNC = "in_sync"
PUSHED = "pushed"
PULLED = "pulled"
CONFLICT = "conflict"
REMOTE_UNREACHABLE = "remote_unreachable"
INVALID_REMOTE = "invalid_remote"
INVALID_LOCAL = "invalid_local"

DEFAULT_BASELINE_PATH = Path("data/risk_log_baseline.json")

REPO, LEAD, RUNTIME = "repo CSV", "lead store", "runtime copy"


@dataclass(frozen=True)
class SyncOutcome:
    action: str
    detail: str = ""
    problems: tuple[str, ...] = ()
    changes: tuple[str, ...] = ()
    dry_run: bool = False


@dataclass(frozen=True)
class Comparison:
    counts: dict[str, int | None]
    remote_reachable: bool
    in_sync: bool
    differences: list[str] = field(default_factory=list)


def fingerprint(risks: list[Risk]) -> str:
    canonical = json.dumps([r.model_dump() for r in sorted(risks, key=lambda r: r.id)], sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def describe_diff(a_name: str, a: list[Risk], b_name: str, b: list[Risk]) -> list[str]:
    """What differs between two logs, one line each, naming the risk and field."""
    left, right = {r.id: r for r in a}, {r.id: r for r in b}
    lines = []
    for risk_id in sorted(set(left) | set(right)):
        if risk_id not in right:
            lines.append(f"{risk_id}: only in the {a_name}")
        elif risk_id not in left:
            lines.append(f"{risk_id}: only in the {b_name}")
        else:
            for name in Risk.model_fields:
                if getattr(left[risk_id], name) != getattr(right[risk_id], name):
                    lines.append(
                        f"{risk_id}: {name} is {getattr(left[risk_id], name)!r} in the {a_name} "
                        f"but {getattr(right[risk_id], name)!r} in the {b_name}"
                    )
    return lines


class RiskLogSync:
    def __init__(self, csv: CsvRiskLog, remote: RemoteRiskLog, *, db_path: str | Path,
                 baseline_path: str | Path = DEFAULT_BASELINE_PATH) -> None:
        self._csv = csv
        self._remote = remote
        self._db = db_path
        self._baseline = Path(baseline_path)

    # --- the runtime copy ----------------------------------------------------------------------------------------

    def runtime_risks(self) -> list[Risk]:
        from pm.adapters.risk_log import RiskLogMock

        return RiskLogMock(self._db).list_risks()

    def _unknown_items(self, risks: list[Risk]) -> list[str]:
        conn = get_connection(self._db)
        try:
            known = {row[0] for row in conn.execute("SELECT id FROM items")}
        except sqlite3.OperationalError:
            return []  # no tracker table in this database: nothing to check against
        finally:
            conn.close()
        return [f"{r.id}: related item {r.related_item_id} is not in the tracker"
                for r in risks if r.related_item_id and r.related_item_id not in known]

    def _refresh_runtime(self, risks: list[Risk]) -> None:
        """Make the database's risks table exactly the repo log (replace, never append)."""
        if self.runtime_risks() == sorted(risks, key=lambda r: r.id):
            return
        conn = get_connection(self._db)
        try:
            conn.execute("DELETE FROM risks")
            conn.executemany(
                "INSERT INTO risks (id, title, description, severity, status, related_item_id, opened_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(r.id, r.title, r.description, r.severity, r.status, r.related_item_id, r.opened_at) for r in risks],
            )
            conn.commit()
        finally:
            conn.close()

    # --- the agreed state ---------------------------------------------------------------------------------------------

    def _read_baseline(self) -> str | None:
        try:
            return json.loads(self._baseline.read_text()).get("fingerprint")
        except (OSError, ValueError):
            return None

    def _write_baseline(self, risks: list[Risk]) -> None:
        self._baseline.parent.mkdir(parents=True, exist_ok=True)
        self._baseline.write_text(json.dumps(
            {"fingerprint": fingerprint(risks), "synced_at": datetime.now(timezone.utc).isoformat()}))

    # --- operations -----------------------------------------------------------------------------------------------------

    def _local(self) -> list[Risk] | SyncOutcome:
        try:
            return self._csv.list_risks()
        except RiskLogDataError as exc:
            return SyncOutcome(INVALID_LOCAL, f"the repo risk log is invalid: {exc}", problems=tuple(str(exc).split("; ")))

    def _remote_risks(self) -> list[Risk] | SyncOutcome:
        try:
            return self._remote.list_risks()
        except RemoteUnavailableError as exc:
            return SyncOutcome(REMOTE_UNREACHABLE, f"the lead-facing store could not be reached: {exc}")

    def pull(self, dry_run: bool = False) -> SyncOutcome:
        """Take the lead's table as the truth: repo CSV and runtime copy become it."""
        local = self._local()
        remote = self._remote_risks()
        if isinstance(remote, SyncOutcome):
            return remote
        return self._apply_pull(local if isinstance(local, list) else [], remote, dry_run)

    def _apply_pull(self, local: list[Risk], remote: list[Risk], dry_run: bool) -> SyncOutcome:
        problems = validate_risks(remote) + self._unknown_items(remote)
        if problems:
            return SyncOutcome(INVALID_REMOTE, "the lead's edits are not valid, so nothing was applied", problems=tuple(problems))
        changes = tuple(describe_diff("repo CSV", local, "lead store", remote))
        if not dry_run:
            self._csv.replace_all(remote)
            self._refresh_runtime(remote)
            self._write_baseline(remote)
        return SyncOutcome(PULLED, f"applied the lead's edits ({len(changes)} difference(s))", changes=changes, dry_run=dry_run)

    def push(self, dry_run: bool = False) -> SyncOutcome:
        """Take the repo CSV as the truth: the lead's table becomes it."""
        local = self._local()
        if isinstance(local, SyncOutcome):
            return local
        remote = self._remote_risks()
        if isinstance(remote, SyncOutcome):
            return remote
        return self._apply_push(local, remote, dry_run)

    def _apply_push(self, local: list[Risk], remote: list[Risk], dry_run: bool) -> SyncOutcome:
        changes = tuple(describe_diff("lead store", remote, "repo CSV", local))
        if not dry_run:
            try:
                self._remote.replace_all(local)
            except RemoteUnavailableError as exc:
                return SyncOutcome(REMOTE_UNREACHABLE, f"the lead-facing store could not be written: {exc}")
            self._refresh_runtime(local)
            self._write_baseline(local)
        return SyncOutcome(PUSHED, f"updated the lead's table ({len(changes)} difference(s))", changes=changes, dry_run=dry_run)

    def sync(self, dry_run: bool = False) -> SyncOutcome:
        local = self._local()
        if isinstance(local, SyncOutcome):
            return local
        remote = self._remote_risks()
        if isinstance(remote, SyncOutcome):
            if not dry_run:
                self._refresh_runtime(local)  # offline: the runtime copy still follows the repo
            return remote

        fl, fr, base = fingerprint(local), fingerprint(remote), self._read_baseline()
        if fl == fr:
            if not dry_run:
                self._refresh_runtime(local)
                if base != fl:
                    self._write_baseline(local)
            return SyncOutcome(IN_SYNC, "the repo log and the lead's table are identical", dry_run=dry_run)

        differences = tuple(describe_diff("repo CSV", local, "lead store", remote))
        if not remote and local:
            if base is None:
                return self._apply_push(local, remote, dry_run)  # a brand new, empty lead table
            return SyncOutcome(
                CONFLICT,
                "the lead's table is empty but the repo log is not: refusing to wipe the system of record "
                "(run `pull` to accept an empty log, or `push` to restore the lead's table)",
                changes=differences, dry_run=dry_run,
            )
        if base is None:
            return SyncOutcome(CONFLICT, "never synced before and the two differ: choose `push` or `pull`. "
                               + "; ".join(differences), changes=differences, dry_run=dry_run)
        if base == fl:
            return self._apply_pull(local, remote, dry_run)  # only the lead's table moved
        if base == fr:
            return self._apply_push(local, remote, dry_run)  # only the repo moved
        return SyncOutcome(
            CONFLICT, "both the repo log and the lead's table changed since the last sync: choose `push` or `pull`. "
            + "; ".join(differences), changes=differences, dry_run=dry_run,
        )

    def compare(self) -> Comparison:
        local = self._local()
        remote = self._remote_risks()
        runtime = self.runtime_risks()
        local_list = local if isinstance(local, list) else None
        remote_list = remote if isinstance(remote, list) else None
        counts = {
            REPO: len(local_list) if local_list is not None else None,
            LEAD: len(remote_list) if remote_list is not None else None,
            RUNTIME: len(runtime),
        }
        differences: list[str] = []
        if local_list is not None and remote_list is not None:
            differences += describe_diff(REPO, local_list, LEAD, remote_list)
        if local_list is not None:
            differences += describe_diff(REPO, local_list, RUNTIME, runtime)
        in_sync = local_list is not None and remote_list is not None and not differences
        return Comparison(counts=counts, remote_reachable=remote_list is not None, in_sync=in_sync, differences=differences)
