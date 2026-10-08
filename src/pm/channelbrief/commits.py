"""The project's real git commits for a channel's brief: which commits were made on the day, and which of them name a tracker item.

Off unless PM_CHANNEL_REPOS says which repositories belong to a channel, e.g.
    PM_CHANNEL_REPOS="p1-agent-test=/path/to/P2_PM_Delivery_Steward,/path/to/P3_Agents;Teams-agent-test="
(channels split by `;`, a channel's repositories by `,`, the channel by its display name or id). A channel with none configured has no commit
sections at all. Read-only, through pm.adapters.code_host_git; a repository that cannot be read leaves the sections out and says so in the audit,
and never takes the brief down.

A commit "names an item" when its message holds a tracker item id (PM-031). Whether that item exists is checked against the tracker: a commit that
names one the tracker does not have is shown as such, never as a link to work.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

from pm.adapters.code_host_git import CodeHostGit, NotAGitRepository
from pm.adapters.tracker import ItemNotFoundError, TrackerMock
from pm.state.moments import parse_moment

logger = logging.getLogger(__name__)

ENV_REPOS = "PM_CHANNEL_REPOS"


@dataclass(frozen=True)
class CommitLine:
    sha: str  # the first 7 characters, as git shows it
    subject: str  # the first line of the message, exactly as written
    author: str  # as git records it
    day: str  # the calendar date in the channel's timezone
    item_ref: str | None  # the tracker item id the message names, or None
    item_known: bool | None  # whether the tracker has that item; None when no item is named


def repos_for(channel_name: str, channel_id: str) -> list[Path]:
    """The repositories configured for this channel (by display name, ignoring case, or by id); empty when none."""
    raw = (os.environ.get(ENV_REPOS) or "").strip()
    for entry in filter(None, (part.strip() for part in raw.split(";"))):
        name, _, paths = entry.partition("=")
        if name.strip().lower() in (channel_name.lower(), channel_id.lower()):
            return [Path(p.strip()) for p in paths.split(",") if p.strip()]
    return []


def commit_lines(repos: list[Path], *, day: date, timezone: str, db_path) -> tuple[tuple[CommitLine, ...], str]:
    """The commits made on `day` (the channel's calendar day) in these repositories, oldest first, and a word for the audit: ok | unreadable."""
    if not repos:
        return (), "off"
    try:
        commits = CodeHostGit(repos).list_commits()
    except NotAGitRepository as exc:
        logger.warning("channel_brief_commits_unreadable error=%s", exc)
        return (), "unreadable"
    tracker = TrackerMock(db_path=db_path)
    zone = ZoneInfo(timezone)
    lines = []
    for c in commits:
        if parse_moment(c.committed_at).astimezone(zone).date() != day:
            continue
        known = None
        if c.item_ref:
            try:
                tracker.get_item(c.item_ref)
                known = True
            except ItemNotFoundError:
                known = False
        lines.append(CommitLine(c.sha[:7], c.message.splitlines()[0] if c.message else "", c.author_id, day.isoformat(), c.item_ref, known))
    return tuple(lines), "ok"
