"""
Code-host adapter -- interface (PM-04).

Narrow interface for everything this agent needs from source control:
recent commits, one commit by sha, and a branch's current state. Agent
logic depends ONLY on this interface -- no git/GitHub/DevOps SDK type
may leak past it. CodeHostMock is the only implementation so far, over
this repo's own seeded commit fixture (PM-01/02, src/pm/seed/build.py).

This seed models a single trunk, not a branch topology (there's no
"branches" concept in the fixture -- just a flat, chronologically
ordered commit history). get_branch_state() therefore only recognises
one ref, "main", covering every seeded commit; any other ref raises
BranchNotFoundError. A real code-host implementation behind this same
interface would resolve actual branch refs.

The master plan's own edge cases for this adapter, and where each one
is actually exercised in the seed:
  - a commit with no item reference               -> commit "b8888bb"
    (PM-03 difficulty 6)
  - one item referenced by a commit but never
    transitioned                                    -> commit "b9999cc"
    references PM-021, which item_transitions never
    moves out of backlog (PM-03 difficulty 7)
"""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from pydantic import BaseModel

from pm.storage.db import DEFAULT_DB_PATH, get_connection

MAIN_REF = "main"


class Commit(BaseModel):
    sha: str
    author_id: str
    message: str
    item_ref: str | None = None
    committed_at: str


class BranchState(BaseModel):
    ref: str
    head_sha: str | None = None
    commit_count: int = 0


class CommitNotFoundError(Exception):
    """Raised by get_commit() for an unknown sha."""


class BranchNotFoundError(Exception):
    """Raised by get_branch_state() for any ref other than MAIN_REF --
    see this module's own docstring on why only one ref exists here."""


class CodeHost(ABC):
    """Read-only access to source control. A narrow interface, not a
    passthrough for a specific host's REST/GraphQL shape."""

    @abstractmethod
    def list_commits(self, since: str | None = None) -> list[Commit]:
        """All commits, oldest first, optionally bounded by since (an
        ISO date/datetime; commits committed_at >= since are returned)."""

    @abstractmethod
    def get_commit(self, sha: str) -> Commit:
        """Raises CommitNotFoundError if sha doesn't exist."""

    @abstractmethod
    def get_branch_state(self, ref: str) -> BranchState:
        """Raises BranchNotFoundError for a ref this mock doesn't know
        (see this module's own docstring: only MAIN_REF exists here)."""


def _row_to_commit(row: sqlite3.Row) -> Commit:
    return Commit(
        sha=row["sha"],
        author_id=row["author_id"],
        message=row["message"],
        item_ref=row["item_ref"],
        committed_at=row["committed_at"],
    )


class CodeHostMock(CodeHost):
    """The only CodeHost implementation so far: a mock over this repo's
    own seeded commits table (PM-01/02). Opens and closes its own
    connection per call, same posture as TrackerMock."""

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        self._db_path = db_path

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = get_connection(self._db_path)
        try:
            yield conn
        finally:
            conn.close()

    def list_commits(self, since: str | None = None) -> list[Commit]:
        with self._conn() as conn:
            if since is not None:
                rows = conn.execute(
                    "SELECT * FROM commits WHERE committed_at >= ? ORDER BY committed_at", (since,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM commits ORDER BY committed_at").fetchall()
        return [_row_to_commit(row) for row in rows]

    def get_commit(self, sha: str) -> Commit:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM commits WHERE sha = ?", (sha,)).fetchone()
        if row is None:
            raise CommitNotFoundError(sha)
        return _row_to_commit(row)

    def get_branch_state(self, ref: str) -> BranchState:
        if ref != MAIN_REF:
            raise BranchNotFoundError(ref)
        with self._conn() as conn:
            (commit_count,) = conn.execute("SELECT COUNT(*) FROM commits").fetchone()
            head_row = conn.execute("SELECT sha FROM commits ORDER BY committed_at DESC LIMIT 1").fetchone()
        head_sha = head_row["sha"] if head_row is not None else None
        return BranchState(ref=MAIN_REF, head_sha=head_sha, commit_count=commit_count)
