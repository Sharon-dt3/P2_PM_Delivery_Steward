"""The estate's per-person per-day nudge cap, shared with P1.

Two agents each politely chasing the same person is how an agent estate becomes a nuisance. P1 caps its own nudges
per person per channel per day (config `nudge_cap_per_day`, counted from the sent rows of its `nudges` table). That
cap cannot see P2, and P2's cannot see P1's, so on its own each could be exactly within its limit while the person
is chased twice the same day.

This is the shared one. A person's nudges for a day are the SENT rows in both ledgers, which have the same shape
(`member_id`, `date`, `sent_at`): P1's `nudges` table (every channel, read-only: P2 never writes to P1's database)
and P2's own. A pending or rejected nudge was never delivered, so it never counts. The cap is checked when a nudge
is planned AND again at the moment it is sent, because a first nudge waits for a person's approval and can be
approved after P1 has already chased the same person that day.

It fails closed. If P1's ledger is configured and cannot be read (missing, locked, no table), nobody is nudged: not
knowing whether P1 already chased someone is a reason to wait, not to send. Only when no P1 ledger is configured AND
none exists at the default location (P1 has never run here) is P1's count zero, and the reading says so.

A person is not always the same id in both systems (P1's live ledger names people by their Microsoft Graph user id;
P2's roster uses its own). The count for a P2 person therefore includes every id they appear under in P1: their own, and
those of P1 members with the same display name (exact, ignoring case and spacing). Two P1 members with one name are
both counted: not knowing which is which, the safe answer is to count both.

"A day" is the project's local date. P1 stores its own channel's local date, so for a P1 channel in another timezone
a nudge near midnight can land on the neighbouring day: the safe direction to be approximate is not to be relied on,
which is why `docs/shared_nudge_cap.md` says what each side counts.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from pm.adapters.teams import P1_REPO_ROOT
from pm.storage.db import DEFAULT_DB_PATH, get_connection

ENV_CAP = "PM_NUDGE_CAP_PER_PERSON_PER_DAY"
ENV_P1_DB = "P1_DB_PATH"
DEFAULT_P1_DB = P1_REPO_ROOT / "data" / "p1_live.db"


class SharedCapUnavailableError(RuntimeError):
    """P1's ledger is configured but could not be read: the cap cannot be checked, so nothing is sent."""


@dataclass(frozen=True)
class CapReading:
    member_id: str
    day: str
    sent_by_p2: int
    sent_by_p1: int
    cap: int
    p1_ledger: str  # "read" | "absent" (no P1 ledger anywhere: P1 has never run here)

    @property
    def total(self) -> int:
        return self.sent_by_p2 + self.sent_by_p1

    @property
    def reached(self) -> bool:
        return self.total >= self.cap

    def describe(self) -> str:
        return (f"{self.member_id} has had {self.total} nudge(s) on {self.day} "
                f"({self.sent_by_p2} from this agent, {self.sent_by_p1} from P1); the shared cap is {self.cap}")


def p1_ledger_path(env: Mapping[str, str] | None = None) -> tuple[Path, bool]:
    """(path, explicitly configured). An explicit P1_DB_PATH that does not exist is an error, not 'no P1'."""
    env = os.environ if env is None else env
    explicit = env.get(ENV_P1_DB)
    return (Path(explicit), True) if explicit else (DEFAULT_P1_DB, False)


def cap_from_environment(default: int, env: Mapping[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    raw = (env.get(ENV_CAP) or "").strip()
    if not raw:
        return default
    if not raw.isdigit():
        raise ValueError(f"{ENV_CAP} must be a whole number of nudges per person per day, not {raw!r}")
    return int(raw)


class SharedNudgeCap:
    def __init__(self, *, db_path: str | Path = DEFAULT_DB_PATH, cap: int = 1, p1_db_path: str | Path | None = None,
                 p1_required: bool | None = None, env: Mapping[str, str] | None = None) -> None:
        self._db_path = db_path
        self.cap = cap
        if p1_db_path is None:
            self._p1_path, self._p1_required = p1_ledger_path(env)
        else:
            self._p1_path, self._p1_required = Path(p1_db_path), True if p1_required is None else p1_required

    # --- reading both ledgers -------------------------------------------------------------------------------------------

    def _p2_display_name(self, member_id: str) -> str | None:
        conn = get_connection(self._db_path)
        try:
            row = conn.execute("SELECT display_name FROM assignees WHERE id = ?", (member_id,)).fetchone()
        finally:
            conn.close()
        return row[0] if row else None

    def _p1_ids_for(self, conn: sqlite3.Connection, member_id: str) -> set[str]:
        """Every id this person appears under in P1: their own, plus P1 members who share their display name."""
        ids = {member_id}
        name = self._p2_display_name(member_id)
        if name:
            try:
                ids |= {r[0] for r in conn.execute("SELECT id FROM members WHERE lower(trim(display_name)) = lower(trim(?))", (name,))}
            except sqlite3.OperationalError as exc:
                if "no such table" not in str(exc):  # no members table: only an exact id can match; anything else is unreadable
                    raise
        return ids

    def _sent_by_p1(self, member_id: str, day: str) -> tuple[int, str]:
        if not self._p1_path.exists():
            if self._p1_required:
                raise SharedCapUnavailableError(f"P1's nudge ledger {self._p1_path} is configured but does not exist")
            return 0, "absent"
        try:
            conn = sqlite3.connect(f"file:{self._p1_path}?mode=ro", uri=True, timeout=5)
            try:
                ids = sorted(self._p1_ids_for(conn, member_id))
                row = conn.execute(
                    f"SELECT COUNT(*) FROM nudges WHERE member_id IN ({','.join('?' * len(ids))}) AND date = ? AND sent_at IS NOT NULL",
                    (*ids, day)).fetchone()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            raise SharedCapUnavailableError(f"P1's nudge ledger could not be read ({type(exc).__name__}: {exc})") from exc
        return row[0], "read"

    def _sent_by_p2(self, member_id: str, day: str) -> int:
        conn = get_connection(self._db_path)
        try:
            return conn.execute("SELECT COUNT(*) FROM nudges WHERE member_id = ? AND date = ? AND sent_at IS NOT NULL",
                                (member_id, day)).fetchone()[0]
        finally:
            conn.close()

    def reading(self, member_id: str, day: str) -> CapReading:
        """Raises SharedCapUnavailableError if P1's ledger is configured and cannot be read."""
        by_p1, state = self._sent_by_p1(member_id, day)
        return CapReading(member_id, day, self._sent_by_p2(member_id, day), by_p1, self.cap, state)

    def allows(self, member_id: str, day: str) -> bool:
        return not self.reading(member_id, day).reached

    # --- this agent's own ledger ---------------------------------------------------------------------------------------------

    def record(self, *, member_id: str, day: str, commitment_id: int | None, proposal_id: str | None, key: str) -> None:
        """A nudge planned for this person: not yet sent, so not yet counted."""
        conn = get_connection(self._db_path)
        try:
            conn.execute("INSERT INTO nudges (member_id, date, commitment_id, proposal_id, idempotency_key) VALUES (?, ?, ?, ?, ?) "
                         "ON CONFLICT(idempotency_key) DO UPDATE SET proposal_id = excluded.proposal_id",
                         (member_id, day, commitment_id, proposal_id, key))
            conn.commit()
        finally:
            conn.close()

    def mark_sent(self, key: str, *, sent_at: str, day: str) -> None:
        """The nudge was delivered: from now on it counts, on the day it actually went."""
        conn = get_connection(self._db_path)
        try:
            conn.execute("UPDATE nudges SET sent_at = ?, date = ? WHERE idempotency_key = ?", (sent_at, day, key))
            conn.commit()
        finally:
            conn.close()

    def ever_nudged(self, member_id: str) -> bool:
        """Has this agent ever delivered a nudge to this person (any day)?"""
        conn = get_connection(self._db_path)
        try:
            return conn.execute("SELECT 1 FROM nudges WHERE member_id = ? AND sent_at IS NOT NULL LIMIT 1", (member_id,)).fetchone() is not None
        finally:
            conn.close()
