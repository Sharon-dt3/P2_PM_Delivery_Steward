"""
Wiring for the chat dependency -- NOT a new adapter implementation.

D11's own scope line is explicit: this agent inherits P1's Teams reader
and publisher outright, with zero new code. Every class used below --
MockTeamsReader, LogPublisher, ScopedTeamsReader, and the TeamsReader/
TeamsPublisher interfaces themselves -- is imported from p1/spine
unchanged. The only thing this module adds is telling those already-
parameterized constructors where THIS repo's copies of things live, the
same kind of config wiring p1.adapters.factory itself does from
environment variables. Nothing here subclasses or reimplements
TeamsReader/TeamsPublisher.

Why not just call p1.adapters.factory.get_teams_reader() directly?
Because that function's own MockTeamsReader.from_fixtures() call takes
no arguments, defaulting to a cwd-relative "seed/fixtures" -- correct
from inside the P3_Agents repo itself, but this repo's own cwd has no
such directory (this repo's own src/pm/seed/fixtures/ holds the P1
OutcomeRecord fixtures, not Teams messages -- see
src/pm/seed/build.py's own docstring on why channel messages are reused
from P1's fixture rather than re-seeded here). P1's own fixtures_dir
parameter already supports pointing elsewhere; get_teams_reader() below
supplies that argument explicitly rather than reimplementing
MockTeamsReader or duplicating P1's fixture data into this repo.
"""

from __future__ import annotations

import os
from pathlib import Path

from p1.adapters.teams_publisher import TeamsPublisher
from p1.adapters.teams_publisher_mock import LogPublisher
from p1.adapters.teams_reader import TeamsReader
from p1.adapters.teams_reader_mock import MockTeamsReader
from p1.governance.scope_gate import ScopedTeamsReader

from pm.storage.db import DEFAULT_DB_PATH

# Both repos are expected to stay checked out as siblings on disk (see
# this repo's own pyproject.toml [tool.uv.sources] comment) --
# overridable via the P1_REPO_ROOT environment variable for anyone who
# checks them out differently.
_REPO_ROOT = Path(__file__).resolve().parents[3]  # .../P2_PM_Delivery_Steward
_DEFAULT_P1_REPO_ROOT = _REPO_ROOT.parent / "P3_Agents"
P1_REPO_ROOT = Path(os.environ.get("P1_REPO_ROOT", str(_DEFAULT_P1_REPO_ROOT)))
P1_FIXTURES_DIR = P1_REPO_ROOT / "seed" / "fixtures"

DEFAULT_LOG_PATH = Path("data/outbound_log.jsonl")


def get_teams_reader(
    *,
    allowlisted_channel_ids: list[str] | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> TeamsReader:
    """P1's own MockTeamsReader, pointed at P1's real fixture data
    (P1_FIXTURES_DIR), wrapped in P1's own ScopedTeamsReader gate --
    exactly the posture p1.adapters.factory.get_teams_reader() takes,
    just wired for this repo's own paths. allowlisted_channel_ids
    defaults to this repo's own seeded channel (src/pm/seed/build.py's
    CHANNEL_ID) since this repo has no config/channels/*.yaml of its
    own -- that's P1's own scope-gate config surface, not duplicated
    here."""
    from pm.seed.build import CHANNEL_ID

    reader = MockTeamsReader.from_fixtures(fixtures_dir=P1_FIXTURES_DIR)
    channel_ids = allowlisted_channel_ids if allowlisted_channel_ids is not None else [CHANNEL_ID]
    return ScopedTeamsReader(reader, channel_ids, db_path=str(db_path))


def get_teams_publisher(log_path: str | Path = DEFAULT_LOG_PATH) -> TeamsPublisher:
    """P1's own LogPublisher, logging to this repo's own
    data/outbound_log.jsonl -- each repo keeps its own inspectable
    outbound log rather than sharing P1's."""
    return LogPublisher(log_path=log_path)
