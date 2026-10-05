"""PM-11, Power Automate half: the scheduled morning job hands its brief to a
Teams publisher chosen by configuration, the way P1's factory chooses it.

Safe by default: with nothing configured the publisher is P1's log-only one,
so nothing leaves the machine. A real (Power Automate) post additionally needs
live posting switched on explicitly and a target channel on P1's allowlist --
until PM-13 replaces this with the approval gate. A failed delivery never
crashes the job, is not recorded as delivered (so a retry can succeed), and one
brief is delivered per channel per local day.

The "flow" in these tests is a real HTTP server on 127.0.0.1 -- the full
request path runs, and nothing can reach Teams.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, time, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from p1.adapters.teams_publisher_mock import LogPublisher
from p1.adapters.teams_publisher_power_automate import PowerAutomateTeamsPublisher

from pm.adapters.teams import get_teams_publisher
from pm.delivery.brief_delivery import (
    ALREADY_DELIVERED,
    DELIVERED,
    FAILED,
    REFUSED_CHANNEL_NOT_ALLOWLISTED,
    REFUSED_LIVE_POSTING_DISABLED,
    DeliveryPolicy,
    load_delivery_policy,
)
from pm.eval.pm12_cases import ScriptedGateway
from pm.jobs.morning_brief_job import (
    GENERATED,
    SKIPPED_NON_WORKING_DAY,
    run_morning_brief_job,
)
from pm.scheduling.config import ProjectScheduleConfig
from pm.scheduling.scheduler import build_scheduler
from pm.seed.build import CHANNEL_ID

WED_08_00_COLOMBO = datetime(2026, 9, 16, 2, 30, tzinfo=timezone.utc)
THU_08_00_COLOMBO = datetime(2026, 9, 17, 2, 30, tzinfo=timezone.utc)
SAT_08_00_COLOMBO = datetime(2026, 9, 19, 2, 30, tzinfo=timezone.utc)
TEST_CHANNEL = "19:test-channel@thread.tacv2"


def _config(**overrides):
    values = {
        "channel_id": CHANNEL_ID, "timezone": "Asia/Colombo", "working_days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
        "morning_brief_time": time(8, 0), "end_of_day_time": time(17, 0),
    }
    return ProjectScheduleConfig(**{**values, **overrides})


def _run(db, publisher, moment=WED_08_00_COLOMBO, policy=None, **kwargs):
    return run_morning_brief_job(
        kwargs.pop("config", _config()), ScriptedGateway(), moment=moment, db_path=db,
        publisher=publisher, policy=policy or DeliveryPolicy(), **kwargs,
    )


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


# --- the job hands its brief over ---------------------------------------------


def test_the_brief_is_handed_to_the_publisher_for_its_channel(seeded_db_path, tmp_path):
    log = LogPublisher(tmp_path / "log.jsonl")

    result = _run(seeded_db_path, log)

    assert result.status == GENERATED and result.delivery_status == DELIVERED
    (row,) = log.read_log()
    assert row["action_type"] == "channel_post" and row["target"] == CHANNEL_ID
    assert row["content"].startswith("Morning brief — 2026-09-16\n\n")
    assert result.brief.content in row["content"]


def test_with_no_publisher_given_the_default_is_log_only(seeded_db_path, tmp_path, monkeypatch):
    monkeypatch.delenv("TEAMS_PUBLISHER_MODE", raising=False)
    monkeypatch.setenv("TEAMS_PUBLISHER_LOG_PATH", str(tmp_path / "default.jsonl"))

    result = run_morning_brief_job(_config(), ScriptedGateway(), moment=WED_08_00_COLOMBO, db_path=seeded_db_path)

    assert result.delivery_status == DELIVERED
    assert len(LogPublisher(tmp_path / "default.jsonl").read_log()) == 1


def test_a_label_and_a_different_target_channel_can_be_configured(seeded_db_path, tmp_path):
    log = LogPublisher(tmp_path / "log.jsonl")
    config = _config(publish_channel_id=TEST_CHANNEL, message_label="[sample data] ")

    _run(seeded_db_path, log, config=config)

    (row,) = log.read_log()
    assert row["target"] == TEST_CHANNEL and row["content"].startswith("[sample data] Morning brief")


def test_a_non_working_day_publishes_nothing(seeded_db_path, tmp_path):
    log = LogPublisher(tmp_path / "log.jsonl")

    result = _run(seeded_db_path, log, moment=SAT_08_00_COLOMBO)

    assert result.status == SKIPPED_NON_WORKING_DAY and result.delivery_status != DELIVERED
    assert log.read_log() == []


def test_one_brief_per_channel_per_local_day(seeded_db_path, tmp_path):
    log = LogPublisher(tmp_path / "log.jsonl")

    first = _run(seeded_db_path, log)
    repeat = _run(seeded_db_path, log)
    later_same_day = _run(seeded_db_path, log, moment=WED_08_00_COLOMBO.replace(minute=45))
    next_day = _run(seeded_db_path, log, moment=THU_08_00_COLOMBO)

    assert [r.delivery_status for r in (first, repeat, later_same_day, next_day)] == [
        DELIVERED, ALREADY_DELIVERED, ALREADY_DELIVERED, DELIVERED,
    ]
    assert len(log.read_log()) == 2


def test_a_deliberate_redelivery_is_possible(seeded_db_path, tmp_path):
    log = LogPublisher(tmp_path / "log.jsonl")

    _run(seeded_db_path, log)
    again = _run(seeded_db_path, log, redeliver=True)

    assert again.delivery_status == DELIVERED and len(log.read_log()) == 2


def test_the_scheduler_gives_the_morning_job_the_publisher_and_the_end_of_day_job_none():
    publisher = object()
    jobs = {j.id: j for j in build_scheduler([_config()], gateway=object(), publisher=publisher).get_jobs()}

    assert jobs[f"pm:morning_brief:{CHANNEL_ID}"].kwargs["publisher"] is publisher
    assert "publisher" not in jobs[f"pm:end_of_day:{CHANNEL_ID}"].kwargs


# --- a real post, against a fake flow ----------------------------------------


class _Flow:
    """A local HTTP server standing in for the Power Automate flow."""

    def __init__(self):
        self.requests: list[dict] = []
        self.status = 200
        flow = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                flow.requests.append(json.loads(body))
                self.send_response(flow.status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok": true}')

            def log_message(self, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/flow"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self):
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture()
def flow():
    f = _Flow()
    yield f
    f.close()


LIVE = DeliveryPolicy(live_posting_enabled=True, allowlisted_channel_ids=[TEST_CHANNEL])


def test_a_real_post_is_refused_unless_live_posting_is_switched_on(seeded_db_path, flow):
    result = _run(seeded_db_path, PowerAutomateTeamsPublisher(flow.url), policy=DeliveryPolicy(
        live_posting_enabled=False, allowlisted_channel_ids=[TEST_CHANNEL]), config=_config(publish_channel_id=TEST_CHANNEL))

    assert result.delivery_status == REFUSED_LIVE_POSTING_DISABLED and flow.requests == []


def test_a_real_post_is_refused_for_a_channel_not_on_the_allowlist(seeded_db_path, flow):
    result = _run(seeded_db_path, PowerAutomateTeamsPublisher(flow.url), policy=LIVE)  # target: the seeded gamma channel

    assert result.delivery_status == REFUSED_CHANNEL_NOT_ALLOWLISTED and flow.requests == []


def test_an_enabled_allowlisted_post_reaches_the_flow_exactly_once(seeded_db_path, flow, monkeypatch):
    monkeypatch.setenv("TEAMS_PUBLISHER_MODE", "power_automate")
    monkeypatch.setenv("POWER_AUTOMATE_FLOW_URL", flow.url)
    config = _config(publish_channel_id=TEST_CHANNEL, message_label="[sample data] ")

    first = _run(seeded_db_path, get_teams_publisher(), policy=LIVE, config=config)
    repeat = _run(seeded_db_path, get_teams_publisher(), policy=LIVE, config=config)

    assert (first.delivery_status, repeat.delivery_status) == (DELIVERED, ALREADY_DELIVERED)
    (request,) = flow.requests
    assert request["action_type"] == "channel_post" and request["target"] == TEST_CHANNEL
    assert request["content"].startswith("[sample data] Morning brief — 2026-09-16") and first.brief.content in request["content"]


def test_a_flow_error_does_not_crash_the_job_and_a_retry_can_still_deliver(seeded_db_path, flow):
    config = _config(publish_channel_id=TEST_CHANNEL)
    publisher = PowerAutomateTeamsPublisher(flow.url)
    flow.status = 500

    failed = _run(seeded_db_path, publisher, policy=LIVE, config=config)

    assert failed.status == GENERATED and failed.brief is not None
    assert failed.delivery_status == FAILED and "500" in failed.delivery_detail

    flow.status = 200
    retried = _run(seeded_db_path, publisher, policy=LIVE, config=config)
    assert retried.delivery_status == DELIVERED


def test_an_unreachable_flow_does_not_crash_the_job(seeded_db_path):
    result = _run(seeded_db_path, PowerAutomateTeamsPublisher("http://127.0.0.1:9/nothing-listens"),
                  policy=LIVE, config=_config(publish_channel_id=TEST_CHANNEL))

    assert result.status == GENERATED and result.delivery_status == FAILED


def test_log_only_needs_no_switch_and_no_allowlist(seeded_db_path, tmp_path):
    result = _run(seeded_db_path, LogPublisher(tmp_path / "log.jsonl"), policy=DeliveryPolicy(
        live_posting_enabled=False, allowlisted_channel_ids=[]))

    assert result.delivery_status == DELIVERED


def test_an_unrecognised_publisher_is_treated_as_live(seeded_db_path):
    class SomethingElse(LogPublisher.__mro__[1]):  # a TeamsPublisher that is not the log-only one
        def post_channel_message(self, channel_id, content):
            raise AssertionError("must not be called")

        def post_direct_message(self, member_id, content):
            raise AssertionError("must not be called")

    result = _run(seeded_db_path, SomethingElse())

    assert result.delivery_status == REFUSED_LIVE_POSTING_DISABLED


# --- the policy from the environment -------------------------------------------


def test_live_posting_is_off_unless_the_switch_is_exactly_on(monkeypatch):
    monkeypatch.delenv("PM_ALLOW_LIVE_POST", raising=False)
    assert load_delivery_policy().live_posting_enabled is False
    for value in ("0", "no", "false", "", "yes please"):
        monkeypatch.setenv("PM_ALLOW_LIVE_POST", value)
        assert load_delivery_policy().live_posting_enabled is False, value
    monkeypatch.setenv("PM_ALLOW_LIVE_POST", "1")
    assert load_delivery_policy().live_posting_enabled is True


def test_the_allowlist_comes_from_p1s_channel_config():
    allowlist = load_delivery_policy().allowlisted_channel_ids

    assert "19:proj-gamma@thread.tacv2" not in allowlist  # the seeded sample channel is not allowlisted
    assert allowlist  # but P1's real test channels are
