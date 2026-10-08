"""The real-git code host: reads commits from git repositories, read-only, behind the same interface as the mock.

A commit names a work item only by a tracker id (PM- and three or more digits); a plan row such as PM-29 is not one.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from pm.adapters.code_host import BranchNotFoundError, CommitNotFoundError
from pm.adapters.code_host_git import CodeHostGit, NotAGitRepository, item_ref_of


def _git(repo: Path, *args: str, when: str | None = None, author: str = "Ada Lovelace") -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": author, "GIT_AUTHOR_EMAIL": "ada@example.test", "GIT_COMMITTER_NAME": author,
           "GIT_COMMITTER_EMAIL": "ada@example.test", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    if when:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = when
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env).stdout


def make_repo(path: Path, commits: list[tuple[str, str, str]], author: str = "Ada Lovelace") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    for i, (message, when, name) in enumerate(commits):
        (path / f"f{i}.txt").write_text(message, encoding="utf-8")
        _git(path, "add", "-A")
        _git(path, "commit", "-q", "-m", message, when=when, author=name or author)
    return path


@pytest.mark.parametrize(("message", "expected"), [
    ("PM-031: fix the staging key", "PM-031"),
    ("fix thing\n\nRefs PM-1042 and PM-031", "PM-1042"),  # the first one named
    ("PM-29: the weekly report", None),  # a plan row, two digits: not a tracker item
    ("pm-031 lowercase", None),
    ("XPM-031 glued", None),
    ("chore: bump the CI image", None),
    ("", None),
])
def test_a_commit_names_an_item_only_by_a_tracker_id(message, expected):
    assert item_ref_of(message) == expected


def test_commits_come_oldest_first_with_author_date_message_and_item_ref(tmp_path):
    repo = make_repo(tmp_path / "r", [
        ("PM-031: first", "2026-10-07T09:00:00+05:30", "Ada Lovelace"),
        ("second, no item", "2026-10-08T10:00:00+05:30", "Grace Hopper"),
    ])

    commits = CodeHostGit([repo]).list_commits()

    assert [(c.message, c.author_id, c.item_ref) for c in commits] == [("PM-031: first", "Ada Lovelace", "PM-031"), ("second, no item", "Grace Hopper", None)]
    assert commits[0].committed_at.startswith("2026-10-07T09:00:00") and len(commits[0].sha) == 40


def test_since_keeps_only_commits_at_or_after_that_moment(tmp_path):
    repo = make_repo(tmp_path / "r", [("old", "2026-10-06T09:00:00+00:00", ""), ("new", "2026-10-08T09:00:00+00:00", "")])

    assert [c.message for c in CodeHostGit([repo]).list_commits(since="2026-10-07")] == ["new"]
    assert [c.message for c in CodeHostGit([repo]).list_commits(since="2026-10-08T09:00:00+00:00")] == ["new"]  # at the moment counts


def test_several_repositories_are_merged_in_time_order(tmp_path):
    a = make_repo(tmp_path / "a", [("a1", "2026-10-07T09:00:00+00:00", ""), ("a2", "2026-10-09T09:00:00+00:00", "")])
    b = make_repo(tmp_path / "b", [("b1", "2026-10-08T09:00:00+00:00", "")])

    assert [c.message for c in CodeHostGit([a, b]).list_commits()] == ["a1", "b1", "a2"]


def test_a_commit_is_found_by_its_full_or_short_sha_and_an_unknown_one_raises(tmp_path):
    repo = make_repo(tmp_path / "r", [("one", "2026-10-07T09:00:00+00:00", "")])
    host = CodeHostGit([repo])
    full = host.list_commits()[0].sha

    assert host.get_commit(full).message == "one" and host.get_commit(full[:7]).message == "one"
    with pytest.raises(CommitNotFoundError):
        host.get_commit("deadbee")


def test_only_main_has_a_state_and_it_is_the_first_repositorys(tmp_path):
    repo = make_repo(tmp_path / "r", [("one", "2026-10-07T09:00:00+00:00", ""), ("two", "2026-10-08T09:00:00+00:00", "")])
    host = CodeHostGit([repo])

    state = host.get_branch_state("main")

    assert state.commit_count == 2 and state.head_sha == host.list_commits()[-1].sha
    with pytest.raises(BranchNotFoundError):
        host.get_branch_state("feature/x")


def test_a_path_that_is_not_a_repository_is_a_named_error_not_a_crash_somewhere_else(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()

    with pytest.raises(NotAGitRepository):
        CodeHostGit([plain]).list_commits()


def test_reading_changes_nothing_in_the_repository(tmp_path):
    repo = make_repo(tmp_path / "r", [("one", "2026-10-07T09:00:00+00:00", "")])
    before = (_git(repo, "rev-parse", "HEAD"), _git(repo, "status", "--porcelain"), sorted(p.name for p in (repo / ".git").iterdir()))

    host = CodeHostGit([repo])
    host.list_commits()
    host.get_commit(host.list_commits()[0].sha)
    host.get_branch_state("main")

    assert (_git(repo, "rev-parse", "HEAD"), _git(repo, "status", "--porcelain"), sorted(p.name for p in (repo / ".git").iterdir())) == before
