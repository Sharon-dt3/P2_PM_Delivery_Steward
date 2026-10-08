"""Code-host adapter over real git repositories on this machine, read-only (PM-04's interface, a real implementation).

Same narrow interface as CodeHostMock (pm.adapters.code_host): recent commits, one commit by sha, the state of `main`. Nothing past it knows git.
It only ever runs `git log`, `git rev-parse` and `git rev-list` (reads), against paths it is given, with no shell, a timeout, and git's own
prompts and optional lock files switched off. It never fetches, never writes, never needs a network.

A commit's `item_ref` is the first tracker item id its message names: `PM-` and at least three digits, as the tracker numbers its items (PM-031).
The plan's own row numbers (PM-29, two digits) are deliberately not item ids, so naming a plan row in a message is not a reference to work.
A message that names no such id has `item_ref = None`: a commit with no item reference. Whether the id names an item that really exists is
the caller's to check against the tracker; this adapter reports what the message says.

`author_id` is the author's name as git records it. It is not a roster id: matching authors to people is not guessed here.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from pm.adapters.code_host import (
    MAIN_REF,
    BranchNotFoundError,
    BranchState,
    CodeHost,
    Commit,
    CommitNotFoundError,
)
from pm.state.moments import parse_moment

ITEM_ID = re.compile(r"\bPM-\d{3,}\b")
_FIELD, _RECORD = "\x1f", "\x1e"
_LOG_FORMAT = f"%H{_FIELD}%an{_FIELD}%cI{_FIELD}%B{_RECORD}"
_TIMEOUT_SECONDS = 20


class NotAGitRepository(Exception):
    """A configured path is not a git repository (or git could not read it)."""


def item_ref_of(message: str) -> str | None:
    """The first tracker item id the message names, else None."""
    found = ITEM_ID.search(message or "")
    return found.group(0) if found else None


class CodeHostGit(CodeHost):
    def __init__(self, repos: list[str | Path]) -> None:
        self._repos = [Path(r).expanduser() for r in repos]

    def _git(self, repo: Path, *args: str) -> str:
        env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"}
        try:
            done = subprocess.run(  # fixed read-only subcommands; the repo path is an argument, never a shell string
                ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, timeout=_TIMEOUT_SECONDS, env=env,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            raise NotAGitRepository(f"{repo}: {type(exc).__name__}") from exc
        return done.stdout

    def _commits_in(self, repo: Path) -> list[Commit]:
        raw = self._git(repo, "log", "--reverse", f"--format={_LOG_FORMAT}")
        commits = []
        for record in raw.split(_RECORD):
            record = record.strip("\n")
            if not record:
                continue
            sha, author, committed_at, message = record.split(_FIELD, 3)
            message = message.strip()
            commits.append(Commit(sha=sha, author_id=author, message=message, item_ref=item_ref_of(message), committed_at=committed_at))
        return commits

    def list_commits(self, since: str | None = None) -> list[Commit]:
        commits = [c for repo in self._repos for c in self._commits_in(repo)]
        if since is not None:
            floor = parse_moment(since)
            commits = [c for c in commits if parse_moment(c.committed_at) >= floor]
        return sorted(commits, key=lambda c: (parse_moment(c.committed_at), c.sha))

    def get_commit(self, sha: str) -> Commit:
        for repo in self._repos:
            for commit in self._commits_in(repo):
                if commit.sha == sha or (len(sha) >= 7 and commit.sha.startswith(sha)):
                    return commit
        raise CommitNotFoundError(sha)

    def get_branch_state(self, ref: str) -> BranchState:
        """`main` of the first configured repository. With several repositories there is no single head, so only the first one answers."""
        if ref != MAIN_REF or not self._repos:
            raise BranchNotFoundError(ref)
        repo = self._repos[0]
        try:
            head = self._git(repo, "rev-parse", "--verify", MAIN_REF).strip()
            count = int(self._git(repo, "rev-list", "--count", MAIN_REF).strip())
        except NotAGitRepository as exc:
            raise BranchNotFoundError(ref) from exc
        return BranchState(ref=ref, head_sha=head, commit_count=count)
