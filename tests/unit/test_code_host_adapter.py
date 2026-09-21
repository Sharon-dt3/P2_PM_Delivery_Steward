from __future__ import annotations

import pytest

from pm.adapters.code_host import (
    MAIN_REF,
    BranchNotFoundError,
    CodeHost,
    CodeHostMock,
    CommitNotFoundError,
)


@pytest.fixture()
def code_host(seeded_db_path) -> CodeHost:
    return CodeHostMock(db_path=seeded_db_path)


def test_list_commits_returns_every_seeded_commit(code_host):
    assert len(code_host.list_commits()) == 12


def test_list_commits_bounded_by_since(code_host):
    commits = code_host.list_commits(since="2026-09-12")
    assert commits
    assert all(commit.committed_at >= "2026-09-12" for commit in commits)


def test_get_commit_surfaces_the_no_item_reference_edge_case(code_host):
    """PM-04's own edge case: a commit with no item reference."""
    commit = code_host.get_commit("b8888bb")
    assert commit.item_ref is None


def test_get_commit_raises_for_unknown_sha(code_host):
    with pytest.raises(CommitNotFoundError):
        code_host.get_commit("deadbeef")


def test_commit_references_an_item_that_was_never_transitioned(code_host):
    """PM-04's own paired edge case: one item referenced by a commit but
    never transitioned -- see src/pm/seed/build.py's own comment on
    PM-021."""
    commit = code_host.get_commit("b9999cc")
    assert commit.item_ref == "PM-021"


def test_get_branch_state_for_main(code_host):
    state = code_host.get_branch_state(MAIN_REF)
    assert state.commit_count == 12
    assert state.head_sha is not None


def test_get_branch_state_raises_for_an_unknown_ref(code_host):
    with pytest.raises(BranchNotFoundError):
        code_host.get_branch_state("feature/does-not-exist")
