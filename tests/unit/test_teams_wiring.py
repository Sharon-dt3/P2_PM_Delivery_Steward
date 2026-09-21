"""PM-04's own DoD line: "P1's Teams reader satisfies the chat
dependency with no new code." src/pm/adapters/teams.py only wires
P1's own classes at the right paths for this repo (see that module's
own docstring) -- these tests exercise that wiring end to end, against
P1's real fixture data on disk, not a stand-in."""

from __future__ import annotations

from pm.adapters.teams import P1_FIXTURES_DIR, P1_REPO_ROOT, get_teams_publisher, get_teams_reader
from pm.seed.build import CHANNEL_ID


def test_p1_repo_root_resolves_to_the_sibling_p3_agents_checkout():
    assert P1_REPO_ROOT.name == "P3_Agents"


def test_p1_fixtures_dir_exists_with_the_expected_files():
    """The one test here that actually reaches into the sibling
    P3_Agents checkout -- confirms the "reused, not re-seeded" fixture
    story is real, not just a path computed and never checked. If this
    fails, P1_REPO_ROOT probably isn't checked out as a sibling folder
    -- override with the P1_REPO_ROOT environment variable if so."""
    assert P1_FIXTURES_DIR.exists(), f"{P1_FIXTURES_DIR} does not exist"
    assert (P1_FIXTURES_DIR / "channels.json").exists()
    assert (P1_FIXTURES_DIR / "members.json").exists()
    assert (P1_FIXTURES_DIR / "messages.json").exists()


def test_get_teams_reader_reads_p1s_real_fixture_data(tmp_path):
    reader = get_teams_reader(db_path=tmp_path / "pm_test.db")

    channels = reader.list_channels()
    assert channels  # non-empty -- proves fixture data is really being read

    page = reader.list_messages(CHANNEL_ID)
    assert page.messages  # the allowlisted channel is readable through the scope gate


def test_get_teams_publisher_logs_to_this_repos_own_path(tmp_path):
    log_path = tmp_path / "outbound_log.jsonl"
    publisher = get_teams_publisher(log_path=log_path)

    result = publisher.post_channel_message(CHANNEL_ID, "test message from P2's adapter wiring check")
    assert result["ok"] is True
    assert log_path.exists()
