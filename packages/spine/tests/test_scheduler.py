"""
Real proof of spine.scheduling against a config shape that has nothing
to do with P1's ChannelConfig -- a "morning brief" spec with a
different field name for its scheduled time entirely, to prove
to_spec()/job_kwargs() genuinely decouples this module from any one
agent's naming.
"""
from __future__ import annotations

from datetime import date, datetime, time, timezone

from spine.scheduling.scheduler import ScheduleSpec, build_scheduler, is_due


def test_is_due_true_at_exact_local_minute_on_a_working_day():
    spec = ScheduleSpec(
        job_id="test:alpha", timezone="America/New_York",
        working_days=["Mon", "Tue", "Wed", "Thu", "Fri"], non_working_dates=[],
        scheduled_time=time(9, 0),
    )
    # 2026-09-21 is a Monday; 13:00 UTC = 09:00 EDT
    moment = datetime(2026, 9, 21, 13, 0, tzinfo=timezone.utc)
    assert is_due(spec, moment) is True


def test_is_due_false_one_minute_off():
    spec = ScheduleSpec(
        job_id="test:alpha", timezone="America/New_York",
        working_days=["Mon", "Tue", "Wed", "Thu", "Fri"], non_working_dates=[],
        scheduled_time=time(9, 0),
    )
    moment = datetime(2026, 9, 21, 13, 1, tzinfo=timezone.utc)
    assert is_due(spec, moment) is False


def test_is_due_false_on_a_non_working_day():
    spec = ScheduleSpec(
        job_id="test:alpha", timezone="America/New_York",
        working_days=["Mon", "Tue", "Wed", "Thu", "Fri"], non_working_dates=[],
        scheduled_time=time(9, 0),
    )
    # 2026-09-19 is a Saturday
    moment = datetime(2026, 9, 19, 13, 0, tzinfo=timezone.utc)
    assert is_due(spec, moment) is False


def test_is_due_false_on_an_excepted_non_working_date():
    spec = ScheduleSpec(
        job_id="test:alpha", timezone="America/New_York",
        working_days=["Mon", "Tue", "Wed", "Thu", "Fri"],
        non_working_dates=[date(2026, 9, 21)],
        scheduled_time=time(9, 0),
    )
    moment = datetime(2026, 9, 21, 13, 0, tzinfo=timezone.utc)
    assert is_due(spec, moment) is False


def test_build_scheduler_wires_one_cron_job_per_config_via_caller_supplied_accessors():
    """A deliberately alien config shape (field named 'brief_time', not
    'daily_digest_time') -- proves this module never assumes P1's own
    field names, only whatever to_spec() extracts."""

    class MorningBriefConfig:
        def __init__(self, project_id: str, brief_time: time, tz: str):
            self.project_id = project_id
            self.brief_time = brief_time
            self.tz = tz

    configs = [MorningBriefConfig("proj-alpha", time(8, 30), "UTC")]
    calls: list[dict] = []

    def fake_job(**kwargs):
        calls.append(kwargs)

    scheduler = build_scheduler(
        configs,
        to_spec=lambda c: ScheduleSpec(
            job_id=f"brief:{c.project_id}", timezone=c.tz,
            working_days=["Mon", "Tue", "Wed", "Thu", "Fri"], non_working_dates=[],
            scheduled_time=c.brief_time,
        ),
        job_fn=fake_job,
        job_kwargs=lambda c: {"project_id": c.project_id},
    )

    job = scheduler.get_job("brief:proj-alpha")
    assert job is not None
    assert job.kwargs == {"project_id": "proj-alpha"}
    # not started, per this module's own contract -- callers start() it themselves
    assert scheduler.running is False
