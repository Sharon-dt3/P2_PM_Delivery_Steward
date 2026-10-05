"""
PM-11: reused scheduling (is_due_for_morning_brief(), build_scheduler())
plus the job body it drives (run_morning_brief_job()).

Row-level acceptance test, literal: "A clock-override run produces the
brief at the simulated time." -- proven directly by
test_clock_override_run_produces_the_brief_at_the_simulated_time below:
a moment that is_due_for_morning_brief() itself says yes to is handed to
run_morning_brief_job(), and the brief that comes back carries that
exact simulated moment as its own facts.as_of, not whatever the real
clock happens to read while the test runs.

AutoGroundedFakeGateway (not a fixed list of canned responses, unlike
tests/unit/test_reporting_morning_brief.py's own FakeGateway) parses
each call's own rendered prompt for the reference_ids its facts_block
actually lists and echoes exactly those back, grounded. This repo's real
seeded data (src/pm/seed/build.py) has many people and facts, and which
ones fall into a given morning brief depends on the moment passed in --
hand-maintaining a fixed response list against that would be exactly the
kind of brittle, drifting fixture this repo's own testing conventions
avoid elsewhere. No real network call happens anywhere in this file.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, time, timezone

from apscheduler.triggers.cron import CronTrigger
from spine.llm.gateway import LLMResponse

from pm.jobs.morning_brief_job import (
    GENERATED,
    SKIPPED_NON_WORKING_DAY,
    run_morning_brief_job,
)
from pm.scheduling.config import ProjectScheduleConfig, default_project_schedule_config
from pm.scheduling.scheduler import build_scheduler, is_due_for_morning_brief
from pm.seed.build import CHANNEL_ID


class AutoGroundedFakeGateway:
    def __init__(self) -> None:
        self.calls = 0

    def generate(self, prompt: str, **kwargs: object) -> LLMResponse:
        self.calls += 1
        # Echo each fact back faithfully: its own detail as the line and as the quote.
        facts = re.findall(r"reference_id:\s*(\S+)\n\s*detail:\s*(.+)", prompt)
        lines = [{"text": detail, "reference_id": ref, "quote": detail} for ref, detail in facts]
        return LLMResponse(
            text=json.dumps({"lines": lines}),
            provider="fake",
            model="fake",
            prompt_tokens=0,
            completion_tokens=0,
            latency_ms=0.0,
            cache_hit=False,
        )


def _config() -> ProjectScheduleConfig:
    return ProjectScheduleConfig(
        channel_id=CHANNEL_ID,
        timezone="Asia/Colombo",
        working_days=["Mon", "Tue", "Wed", "Thu", "Fri"],
        non_working_dates=[],
        morning_brief_time=time(8, 0),
        end_of_day_time=time(17, 0),
    )


# --- pm.scheduling.config ---------------------------------------------------


def test_default_project_schedule_config_reads_the_real_proj_gamma_timezone_and_working_days():
    """Proves genuine reuse of P1's own config/channels/proj-gamma.yaml
    rather than a second, hand-typed copy: if that file's timezone or
    working_days ever changed, this test would fail until this repo's
    own schedule followed."""
    config = default_project_schedule_config()
    assert config.channel_id == CHANNEL_ID
    assert config.timezone == "Asia/Colombo"
    assert config.working_days == ["Mon", "Tue", "Wed", "Thu", "Fri"]


# --- is_due_for_morning_brief ------------------------------------------------


def test_is_due_true_at_the_exact_local_minute_on_a_working_day():
    config = _config()
    # 2026-09-16 is a Wednesday. 02:30 UTC == 08:00 Asia/Colombo (UTC+5:30).
    moment = datetime(2026, 9, 16, 2, 30, tzinfo=timezone.utc)
    assert is_due_for_morning_brief(config, moment) is True


def test_is_due_false_on_a_non_working_day_even_at_the_exact_time():
    config = _config()
    # 2026-09-19 is a Saturday.
    moment = datetime(2026, 9, 19, 2, 30, tzinfo=timezone.utc)
    assert is_due_for_morning_brief(config, moment) is False


# --- run_morning_brief_job ---------------------------------------------------


def test_clock_override_run_produces_the_brief_at_the_simulated_time(seeded_db_path):
    """PM-11's own acceptance test, literal: "A clock-override run
    produces the brief at the simulated time.\""""
    config = _config()
    simulated_moment = datetime(2026, 9, 16, 2, 30, tzinfo=timezone.utc)
    assert is_due_for_morning_brief(config, simulated_moment) is True

    gateway = AutoGroundedFakeGateway()
    result = run_morning_brief_job(config, gateway, moment=simulated_moment, db_path=seeded_db_path)

    assert result.status == GENERATED
    assert result.taken_at == simulated_moment.isoformat()
    assert result.brief is not None
    assert result.brief.facts.as_of == simulated_moment.isoformat()
    assert gateway.calls > 0
    assert result.brief.content  # non-empty: real prose was actually generated
    assert "[as recorded]" not in result.brief.content  # every line grounded; none fell back
    assert not any(result.brief.dropped.values())


def test_a_repeat_run_for_the_same_simulated_moment_reuses_the_persisted_snapshot(seeded_db_path):
    """save_snapshot()'s own DuplicateSnapshotError is what makes this
    idempotent at the snapshot layer -- a second call for the identical
    moment must not raise, and must return the same facts as the first."""
    config = _config()
    simulated_moment = datetime(2026, 9, 16, 2, 30, tzinfo=timezone.utc)

    first = run_morning_brief_job(config, AutoGroundedFakeGateway(), moment=simulated_moment, db_path=seeded_db_path)
    second = run_morning_brief_job(config, AutoGroundedFakeGateway(), moment=simulated_moment, db_path=seeded_db_path)

    assert first.status == second.status == GENERATED
    assert first.brief.facts == second.brief.facts


def test_a_non_working_day_moment_is_skipped_without_calling_the_model(seeded_db_path):
    config = _config()
    # 2026-09-19 is a Saturday.
    simulated_moment = datetime(2026, 9, 19, 2, 30, tzinfo=timezone.utc)

    gateway = AutoGroundedFakeGateway()
    result = run_morning_brief_job(config, gateway, moment=simulated_moment, db_path=seeded_db_path)

    assert result.status == SKIPPED_NON_WORKING_DAY
    assert result.brief is None
    assert gateway.calls == 0


# --- build_scheduler ---------------------------------------------------------


def _field(trigger: CronTrigger, name: str) -> str:
    return next(str(f) for f in trigger.fields if f.name == name)


def test_build_scheduler_wires_one_morning_brief_job_per_project():
    configs = [
        ProjectScheduleConfig(
            channel_id="proj-x",
            timezone="Asia/Tokyo",
            working_days=["Mon", "Tue", "Wed", "Thu", "Fri"],
            morning_brief_time=time(8, 0),
            end_of_day_time=time(17, 0),
        ),
        ProjectScheduleConfig(
            channel_id="proj-y",
            timezone="Europe/London",
            working_days=["Mon", "Wed", "Fri"],
            morning_brief_time=time(7, 30),
            end_of_day_time=time(16, 0),
        ),
    ]
    scheduler = build_scheduler(configs, gateway=object())
    jobs = {job.id: job for job in scheduler.get_jobs()}

    assert set(jobs) == {
        "pm:morning_brief:proj-x", "pm:morning_brief:proj-y", "pm:end_of_day:proj-x", "pm:end_of_day:proj-y",
    }

    trigger_x = jobs["pm:morning_brief:proj-x"].trigger
    assert str(trigger_x.timezone) == "Asia/Tokyo"
    assert _field(trigger_x, "hour") == "8"
    assert _field(trigger_x, "minute") == "0"
    assert _field(trigger_x, "day_of_week") == "mon,tue,wed,thu,fri"


def test_build_scheduler_never_starts_the_scheduler():
    scheduler = build_scheduler([_config()], gateway=object())
    assert scheduler.running is False


def test_build_scheduler_registers_one_end_of_day_job_per_project_too():
    """PM-11 is "morning and end of day": both jobs exist. The end-of-day one
    captures the end-of-day snapshot only (see pm.jobs.end_of_day_job);
    PM-22 adds the summary."""
    scheduler = build_scheduler([_config()], gateway=object())
    job_ids = {job.id for job in scheduler.get_jobs()}
    assert job_ids == {f"pm:morning_brief:{CHANNEL_ID}", f"pm:end_of_day:{CHANNEL_ID}"}
