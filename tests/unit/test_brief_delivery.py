"""PM-11/PM-13: which Teams publisher the system uses -- chosen by configuration
the way P1's factory chooses it. Log-only unless explicitly set otherwise.

What may be posted, and by whom, is the approval gate's business
(test_approval_service.py); this file is only the choice of publisher.
"""

from __future__ import annotations

import pytest
from p1.adapters.teams_publisher_mock import LogPublisher
from p1.adapters.teams_publisher_power_automate import PowerAutomateTeamsPublisher

from pm.adapters.teams import get_teams_publisher

# --- choosing the publisher -------------------------------------------------


def test_with_nothing_configured_the_publisher_is_the_log_only_one(monkeypatch, tmp_path):
    monkeypatch.delenv("TEAMS_PUBLISHER_MODE", raising=False)

    assert isinstance(get_teams_publisher(log_path=tmp_path / "log.jsonl"), LogPublisher)


def test_mock_mode_is_the_log_only_one_and_honours_the_log_path_setting(monkeypatch, tmp_path):
    monkeypatch.setenv("TEAMS_PUBLISHER_MODE", "mock")
    monkeypatch.setenv("TEAMS_PUBLISHER_LOG_PATH", str(tmp_path / "from_env.jsonl"))

    get_teams_publisher().post_channel_message("c", "hello")

    assert (tmp_path / "from_env.jsonl").exists()


def test_power_automate_mode_without_a_flow_url_fails_loudly(monkeypatch):
    monkeypatch.setenv("TEAMS_PUBLISHER_MODE", "power_automate")
    monkeypatch.delenv("POWER_AUTOMATE_FLOW_URL", raising=False)

    with pytest.raises(RuntimeError, match="POWER_AUTOMATE_FLOW_URL"):
        get_teams_publisher()


def test_power_automate_mode_with_a_flow_url_is_p1s_real_publisher(monkeypatch):
    monkeypatch.setenv("TEAMS_PUBLISHER_MODE", "power_automate")
    monkeypatch.setenv("POWER_AUTOMATE_FLOW_URL", "http://127.0.0.1:9/never-called")

    assert isinstance(get_teams_publisher(), PowerAutomateTeamsPublisher)


def test_an_unknown_mode_is_an_error_not_a_silent_default(monkeypatch):
    monkeypatch.setenv("TEAMS_PUBLISHER_MODE", "carrier-pigeon")

    with pytest.raises(ValueError, match="carrier-pigeon"):
        get_teams_publisher()
