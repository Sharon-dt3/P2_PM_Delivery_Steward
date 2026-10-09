"""PM-35, the edge-case and failure pass: each scenario is tested, and none fabricates.

Seven situations (an empty day, a person with no activity, an item that moved twice, malformed model output, the rate-limit path, an unassigned item, a commit
with no item reference) are run through the real pipeline by `pm.eval.pm35_cases` and every claim in the reports is checked against the stored snapshots.

Three kinds of test, because "found nothing" proves little on its own:
  - each scenario comes back clean (and is wired into GC2, the one fabrication number);
  - each CHECK is shown to fire: a report with one claim changed is flagged, so a clean scenario means something;
  - the failures themselves (a model that cannot be reached, one that answers badly) are tested at the level the user meets them: the brief, the jobs, the weekly report.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, time, timezone

import pytest
from p1.adapters.teams_publisher_mock import LogPublisher
from spine.approval.proposals import PENDING, ProposalStore
from spine.llm.gateway import LLMGateway, LLMGatewayError

from pm.approval.proposals import PROPOSED
from pm.approval.service import ApprovalPolicy
from pm.eval import pm12_cases
from pm.eval import pm35_cases as edge
from pm.eval.pm12_cases import ScriptedGateway, measure_gc2
from pm.jobs import weekly_report_job
from pm.jobs.end_of_day_job import SUMMARISED, run_end_of_day_job
from pm.jobs.morning_brief_job import GENERATED, run_morning_brief_job
from pm.reporting.facts import compute_morning_brief_facts
from pm.reporting.morning_brief import (
    MODEL_FAILED,
    generate_morning_brief,
    sections_the_model_failed,
)
from pm.reporting.scripted_summary import ScriptedSummaryGateway
from pm.reporting.scripted_weekly import ScriptedWeeklyGateway
from pm.reporting.weekly import compute_weekly_facts
from pm.reporting.weekly_narrative import generate_narrative
from pm.scheduling.config import ProjectScheduleConfig
from pm.seed.build import CHANNEL_ID
from pm.state.snapshot import build_current_snapshot

MANUAL = ApprovalPolicy(approver_ids=frozenset({"sharon.silva"}))
TZ = "Asia/Colombo"


@pytest.fixture(autouse=True)
def _quiet_pipeline(caplog):
    caplog.set_level("ERROR")  # the pipeline logs every refused line and every failed section; these tests are about what it produces


# --- every scenario comes back clean, and is part of GC2 -----------------------------------------------------------------------------------


@pytest.mark.parametrize("name", list(edge.SCENARIOS))
def test_the_scenario_finds_no_progress_the_snapshots_do_not_support(name):
    assert edge.SCENARIOS[name]() == []


def test_the_seven_scenarios_the_plan_names_are_all_here():
    assert set(edge.SCENARIOS) == {"edge: empty day", "edge: person with no activity", "edge: item that moved twice", "edge: malformed model output",
                                   "edge: rate-limit path", "edge: unassigned item", "edge: commit with no item reference"}


def test_each_scenario_is_a_probe_inside_gc2_and_the_headline_number_stays_a_hard_zero():
    edge.register()
    edge.register()  # wiring the harness twice must not run a scenario twice
    names = [name for name, _ in pm12_cases.GC2_EXTRA_PROBES]
    assert all(names.count(name) == 1 for name in edge.SCENARIOS) and set(edge.SCENARIOS) <= set(names)

    result = {m.metric_id: m for m in measure_gc2()}["GC2-fabricated-claim-count"]

    assert result.measured == 0 and result.passed
    assert all(f"{name} 0" in result.detail for name in edge.SCENARIOS)  # each one ran, and found none


def test_a_scenario_that_makes_the_pipeline_raise_is_a_problem_not_a_pass(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("the database went away")

    monkeypatch.setattr(edge, "run_day", boom)

    problems = edge.SCENARIOS["edge: unassigned item"]()

    assert problems == ["the pipeline raised RuntimeError: the database went away"]


# --- the checks fire: one changed claim in an otherwise clean report is found ------------------------------------------------------------------


@pytest.fixture()
def morning_day(tmp_path):
    return edge.run_day(edge.fresh_db(tmp_path))


def morning_problems(day, content):
    return edge.unsupported_morning_progress(content, day.morning)


def test_a_clean_brief_has_no_unsupported_claim(morning_day):
    assert morning_problems(morning_day, morning_day.brief.content) == []


def test_a_pending_item_shown_as_delivered_is_found(morning_day):
    content = morning_day.brief.content.replace("- Pending: Aisha Rahman: PM-017", "- Delivered: Aisha Rahman: PM-017", 1)

    assert any("PM-017 is shown as delivered" in p for p in morning_problems(morning_day, content))


def test_a_delivered_item_shown_as_pending_or_blocked_is_found(morning_day):
    pending = morning_day.brief.content.replace("- Delivered: Aisha Rahman: PM-009", "- Pending: Aisha Rahman: PM-009", 1)
    blocked = morning_day.brief.content.replace("- Delivered: Aisha Rahman: PM-009", "- Blocked: Aisha Rahman: PM-009", 1)

    assert any("PM-009 is shown as pending" in p for p in morning_problems(morning_day, pending))
    assert any("PM-009 is shown as blocked" in p for p in morning_problems(morning_day, blocked))


def test_progress_credited_to_the_wrong_person_is_found(morning_day):
    mateo = morning_day.brief.content.replace("## Mateo Silva\n", "## Mateo Silva\n- Delivered: Mateo Silva: PM-009 (Write onboarding wizard integration tests).\n", 1)

    assert any("PM-009 is credited to them" in p and "aisha.rahman" in p for p in morning_problems(morning_day, mateo))


def test_an_item_that_is_not_in_the_snapshot_is_found(morning_day):
    content = morning_day.brief.content.replace("## Mateo Silva\n", "## Mateo Silva\n- Delivered: Mateo Silva: PM-777 (Something that never existed).\n", 1)

    assert any("PM-777, which is not in the snapshot" in p for p in morning_problems(morning_day, content))


def test_a_wrong_sprint_count_is_found(morning_day):
    content = morning_day.brief.content.replace("3 of 17 items in this sprint are done", "9 of 17 items in this sprint are done", 1)

    assert any("9 of 17" in p and "3 of 17" in p for p in morning_problems(morning_day, content))


def test_an_assigned_item_listed_as_unassigned_is_found(morning_day):
    content = morning_day.brief.content.replace("## Nobody is assigned\n", "## Nobody is assigned\n- PM-009 (Write onboarding wizard integration tests): nobody is assigned; status done.\n", 1)

    assert any("PM-009 is listed as unassigned" in p for p in morning_problems(morning_day, content))


def test_a_finished_item_listed_as_work_nobody_is_assigned_to_is_found(tmp_path):
    db = edge.fresh_db(tmp_path)
    edge.add_item(db, "PM-461", "Finished long ago", "done", [("in_progress", "done", "2026-09-10T09:00:00+00:00")], assignee=None)
    day = edge.run_day(db)

    assert "PM-461" not in day.brief.content.split("## Nobody is assigned", 1)[1].split("\n## ", 1)[0]  # the brief leaves it out
    content = day.brief.content.replace("## Nobody is assigned\n", "## Nobody is assigned\n- PM-461 (Finished long ago): nobody is assigned; status done.\n", 1)

    assert any("PM-461 is listed as work nobody is assigned to, but the snapshot has it done" in p for p in morning_problems(day, content))


def test_a_commit_shown_as_tied_to_no_work_that_the_snapshot_ties_to_an_item_is_found(morning_day):
    tied = next(c for c in morning_day.morning.commits if c.item_ref)
    content = morning_day.brief.content.replace("## Commits with no item reference\n", f"## Commits with no item reference\n- {tied.sha[:7]}: {tied.message} (Wei Chen, 2026-09-16).\n", 1)

    assert any(f"ties it to {tied.item_ref}" in p for p in morning_problems(morning_day, content))
    unknown = morning_day.brief.content.replace("## Commits with no item reference\n", "## Commits with no item reference\n- 1234567: made up (Wei Chen, 2026-09-16).\n", 1)
    assert any("1234567 is shown, but it is not in the snapshot" in p for p in morning_problems(morning_day, unknown))


@pytest.fixture()
def eod_day(tmp_path):
    db = edge.fresh_db(tmp_path)
    d = "2026-09-16T"
    edge.add_item(db, "PM-401", "Ship the export button", "done", [("in_progress", "done", d + "06:00:00+00:00")])
    edge.add_item(db, "PM-402", "Wire the audit log", "blocked", [("in_progress", "blocked", d + "07:00:00+00:00")])
    edge.add_item(db, "PM-403", "Done, reopened, done again", "done", [("in_progress", "done", "2026-09-15T09:00:00+00:00"),
                                                                      ("done", "in_progress", d + "03:00:00+00:00"), ("in_progress", "done", d + "10:00:00+00:00")])
    return edge.run_day(db)


def eod_problems(day, content):
    return edge.unsupported_eod_progress(content, day.morning, day.evening)


def test_a_clean_summary_has_no_unsupported_claim(eod_day):
    assert eod_problems(eod_day, eod_day.summary.content) == []
    assert "PM-401" in eod_day.summary.content and "PM-403" in eod_day.summary.content  # the fixture really has what the next tests tamper with


def test_an_item_that_was_not_done_shown_as_shipped_is_found(eod_day):
    line = next(x for x in eod_day.summary.content.splitlines() if x.startswith("- PM-402 "))
    content = eod_day.summary.content.replace("## What shipped\n", f"## What shipped\n{line}\n", 1)

    assert any("PM-402 is shown as shipped today" in p for p in eod_problems(eod_day, content))


def test_a_done_item_shown_as_still_pending_or_newly_blocked_is_found(eod_day):
    pending = eod_day.summary.content.replace("## What is still pending\n", '## What is still pending\n- PM-401 moved from in_progress to in_review. Title: "x". Owner: Aisha Rahman.\n', 1)
    blocked = eod_day.summary.content.replace("## What is newly blocked\n", '## What is newly blocked\n- PM-401 moved from in_progress to blocked. Title: "x". Owner: Aisha Rahman.\n', 1)

    assert any("PM-401 is shown as still pending" in p for p in eod_problems(eod_day, pending))
    assert any("PM-401 is shown as newly blocked" in p for p in eod_problems(eod_day, blocked))


def test_an_item_that_was_already_done_this_morning_shown_as_shipped_is_found(eod_day):
    content = eod_day.summary.content.replace("## What shipped\n", '## What shipped\n- PM-403 moved from in_progress to done. Title: "x". Owner: Aisha Rahman.\n', 1)

    assert any("PM-403 is shown as shipped today" in p for p in eod_problems(eod_day, content))


def test_a_move_the_snapshots_do_not_show_is_found(eod_day):
    content = eod_day.summary.content.replace("PM-401 moved from in_progress to done", "PM-401 moved from backlog to done", 1)

    assert any("PM-401 is said to have moved backlog -> done" in p for p in eod_problems(eod_day, content))


def test_an_item_in_neither_snapshot_is_found(eod_day):
    content = eod_day.summary.content.replace("## What shipped\n", '## What shipped\n- PM-777 moved from in_progress to done. Title: "x". Owner: nobody.\n', 1)

    assert any("PM-777, which is in neither snapshot" in p for p in eod_problems(eod_day, content))


def test_a_wrong_count_of_unchanged_items_is_found(eod_day):
    count = re.search(r"(\d+) other open items did not change", eod_day.summary.content)[1]
    content = eod_day.summary.content.replace(f"{count} other open items", f"{int(count) + 4} other open items", 1)

    assert any("did not change" in p for p in eod_problems(eod_day, content))


# --- the item that moved twice, said by the summary --------------------------------------------------------------------------------------------


def test_an_item_blocked_then_done_is_one_shipped_line_that_shows_the_path(tmp_path):
    db = edge.fresh_db(tmp_path)
    edge.add_item(db, "PM-411", "Blocked, then shipped", "done", [("in_progress", "blocked", "2026-09-16T04:00:00+00:00"), ("blocked", "done", "2026-09-16T09:00:00+00:00")])

    content = edge.run_day(db).summary.content

    assert content.count("PM-411") == 1
    line = next(x for x in content.splitlines() if x.startswith("- PM-411 "))
    assert "in_progress -> blocked -> done" in line and line in content.split("## What shipped")[1].split("## ")[0]


def test_an_item_blocked_all_day_that_flapped_is_not_newly_blocked(tmp_path):
    db = edge.fresh_db(tmp_path)
    edge.add_item(db, "PM-431", "Blocked all day, flapped", "blocked", [("in_progress", "blocked", "2026-09-15T09:00:00+00:00"),
                                                                       ("blocked", "in_progress", "2026-09-16T04:00:00+00:00"), ("in_progress", "blocked", "2026-09-16T09:00:00+00:00")])

    content = edge.run_day(db).summary.content

    assert "PM-431" not in content.split("## What is newly blocked", 1)[1].split("\n## ", 1)[0]
    assert "PM-431 churned (blocked -> in_progress -> blocked)" in content


def test_done_then_reopened_then_done_is_not_shipped_today_and_a_reopened_item_is_not_shipped_either(tmp_path):
    day = edge.run_day(_db_with_churn(tmp_path))

    shipped = day.summary.content.split("## What shipped", 1)[1].split("\n## ", 1)[0]
    assert "PM-421" not in shipped and "PM-422" not in shipped
    assert "PM-421 churned (done -> in_progress -> done)" in day.summary.content and "PM-422 churned (in_progress -> done -> in_progress)" in day.summary.content


def _db_with_churn(tmp_path):
    db = edge.fresh_db(tmp_path)
    edge.add_item(db, "PM-421", "Done, reopened, done again", "done", [("in_progress", "done", "2026-09-15T09:00:00+00:00"),
                                                                       ("done", "in_progress", "2026-09-16T03:00:00+00:00"), ("in_progress", "done", "2026-09-16T10:00:00+00:00")])
    edge.add_item(db, "PM-422", "Done, then reopened", "in_progress", [("in_progress", "done", "2026-09-16T03:30:00+00:00"), ("done", "in_progress", "2026-09-16T10:30:00+00:00")])
    return db


# --- the empty day and the quiet person ---------------------------------------------------------------------------------------------------------


def test_a_project_with_nothing_in_it_gets_reports_that_name_no_item_and_the_summary_never_calls_the_model(tmp_path):
    db = edge.fresh_db(tmp_path)
    edge.empty_the_project(db)
    summary_gateway = ScriptedSummaryGateway()

    day = edge.run_day(db, summary_gateway=summary_gateway)

    assert summary_gateway.calls == 0
    assert not re.search(r"\bPM-\d+", day.brief.content + day.summary.content)
    assert edge.problems_in(day) == []


def test_a_quiet_day_in_a_full_project_reports_no_change_and_never_calls_the_model(tmp_path):
    db = edge.fresh_db(tmp_path)
    summary_gateway = ScriptedSummaryGateway()

    day = edge.run_day(db, summary_gateway=summary_gateway, start=edge.QUIET_FROM, end=edge.QUIET_TO)

    assert summary_gateway.calls == 0
    assert day.summary.content.count("- none.") == 4 and "moved from" not in day.summary.content


def test_a_person_with_no_activity_is_never_given_to_the_model_and_is_said_to_have_no_update(tmp_path):
    db = edge.fresh_db(tmp_path)
    import sqlite3

    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO assignees (id, display_name) VALUES ('quiet.new', 'Quinn Quiet')")
    conn.commit()
    conn.close()
    gateway = edge._PromptLog(ScriptedGateway())

    day = edge.run_day(db, brief_gateway=gateway)

    assert "## Quinn Quiet\n- No update: no tracker activity or commits recorded." in day.brief.content
    assert not any("Quinn Quiet" in prompt or "quiet.new" in prompt for prompt in gateway.prompts)
    assert "Quinn Quiet" not in day.summary.content


# --- the unassigned item and the commit with no item reference ---------------------------------------------------------------------------------------


def test_an_unassigned_finished_item_is_credited_to_nobody_and_says_so(tmp_path):
    db = edge.fresh_db(tmp_path)
    edge.add_item(db, "PM-431", "Nobody's finished item", "done", [("in_progress", "done", "2026-09-16T06:00:00+00:00")], assignee=None)

    day = edge.run_day(db)

    people = day.brief.content.split("## Nobody is assigned", 1)[0]
    assert "PM-431" not in people  # not "Delivered" for anyone
    assert "Owner: unassigned" in next(x for x in day.summary.content.splitlines() if x.startswith("- PM-431 "))
    assert edge.problems_in(day) == []


def test_a_commit_that_names_no_item_changes_no_item_and_is_shown_as_a_commit_with_no_item_reference(tmp_path):
    db = edge.fresh_db(tmp_path)
    edge.add_commit(db, "ab00001", "wei.chen", "finished the export fix, ready to ship", None)
    edge.add_commit(db, "ab00002", "noah.becker", "PM-999: wire up the new queue", None)

    day = edge.run_day(db, start=edge.QUIET_FROM, end=edge.QUIET_TO)

    section = day.brief.content.split("## Commits with no item reference", 1)[1].split("\n## ", 1)[0]
    assert "ab00001: finished the export fix" in section and "ab00002: PM-999: wire up the new queue" in section
    assert {i.id: i.status for i in day.morning.items} == {i.id: i.status for i in day.evening.items}
    assert "PM-999" not in day.summary.content and edge.problems_in(day) == []


# --- the model cannot be reached ---------------------------------------------------------------------------------------------------------------


def test_with_the_model_unreachable_the_brief_is_still_made_from_the_recorded_facts_and_the_model_is_asked_once(tmp_path):
    db = edge.fresh_db(tmp_path)
    gateway = edge.UnavailableGateway()
    snapshot = edge.capture(db, edge.MORNING)

    brief = generate_morning_brief(compute_morning_brief_facts(snapshot), gateway)

    assert gateway.calls == 1  # one outage costs one round of retries, not one per section
    assert "PM-009" in brief.content and "PM-024" in brief.content and "RISK-002" in brief.content
    assert sections_the_model_failed(brief.dropped)
    skipped = [f for failures in brief.dropped.values() for f in failures if f["reason"] == MODEL_FAILED and "not tried again" in f["detail"]]
    assert skipped and all("rate-limited" in f["detail"] for f in skipped)
    assert edge.unsupported_morning_progress(brief.content, snapshot) == []


def test_a_model_that_fails_part_way_leaves_the_earlier_sections_in_the_models_words(tmp_path):
    db = edge.fresh_db(tmp_path)
    snapshot = edge.capture(db, edge.MORNING)

    brief = generate_morning_brief(compute_morning_brief_facts(snapshot), edge.UnavailableAfter(ScriptedGateway(), 2))

    assert brief.sections["sprint_scope"] and brief.sections["committed"]  # answered before the outage
    assert not brief.sections["delivered"] and "delivered" in sections_the_model_failed(brief.dropped)
    assert "sprint_scope" not in sections_the_model_failed(brief.dropped)
    assert edge.unsupported_morning_progress(brief.content, snapshot) == []


def test_one_malformed_section_does_not_stop_the_others(tmp_path):
    db = edge.fresh_db(tmp_path)
    snapshot = edge.capture(db, edge.MORNING)

    class BrokenForOneSection:
        calls = 0

        def generate(self, prompt, **kwargs):
            if "blockers, ranked by impact" in prompt:
                return edge._reply("this is not JSON")
            return ScriptedGateway().generate(prompt, **kwargs)

    brief = generate_morning_brief(compute_morning_brief_facts(snapshot), BrokenForOneSection())

    assert sections_the_model_failed(brief.dropped) == ["blockers"]  # three malformed answers, then the facts as recorded
    assert brief.sections["delivered"]  # the other sections were not touched
    assert "RISK-002" in brief.content and edge.unsupported_morning_progress(brief.content, snapshot) == []


@pytest.mark.parametrize("name", list(edge.MALFORMED_OUTPUTS))
def test_malformed_model_output_loses_no_fact_and_invents_none(tmp_path, name):
    db = edge.fresh_db(tmp_path)
    edge.add_item(db, "PM-441", "Ship the export button", "done", [("in_progress", "done", "2026-09-16T06:00:00+00:00")])
    raw = edge.MALFORMED_OUTPUTS[name]

    day = edge.run_day(db, brief_gateway=edge.RawGateway(raw), summary_gateway=edge.RawGateway(raw))

    assert edge.problems_in(day) == []
    assert "PM-999" not in day.brief.content + day.summary.content
    assert "PM-009" in day.brief.content and "PM-441" in day.summary.content.split("## What shipped")[1].split("\n## ")[0]


def test_the_real_gateway_giving_up_is_handled_like_any_unreachable_model(tmp_path, monkeypatch):
    """The exception here is the one spine's own LLMGateway.generate raises after its rate-limit retries and its local fallback are both spent."""
    db = edge.fresh_db(tmp_path)
    snapshot = edge.capture(db, edge.MORNING)
    gateway = LLMGateway(provider="bedrock", cache_dir=tmp_path / "cache", call_log_path=tmp_path / "calls.jsonl")
    attempts: list[str] = []

    def spent(*args, **kwargs):
        attempts.append("call")
        raise LLMGatewayError("bedrock rate-limited after 4 attempts")

    monkeypatch.setattr(gateway, "_call_bedrock", spent)
    monkeypatch.setattr(gateway, "_call_ollama", spent)

    brief = generate_morning_brief(compute_morning_brief_facts(snapshot), gateway)

    assert len(attempts) == 2  # Bedrock, then the local fallback: once, for the whole brief
    assert "PM-009" in brief.content and sections_the_model_failed(brief.dropped)
    assert edge.unsupported_morning_progress(brief.content, snapshot) == []


# --- the jobs ----------------------------------------------------------------------------------------------------------------------------------


def _config():
    return ProjectScheduleConfig(channel_id=CHANNEL_ID, timezone=TZ, working_days=["Mon", "Tue", "Wed", "Thu", "Fri"],
                                 morning_brief_time=time(8, 0), end_of_day_time=time(17, 0))


def test_the_morning_job_with_the_model_unreachable_still_proposes_the_brief_and_says_why_it_reads_as_recorded(seeded_db_path, tmp_path):
    log = LogPublisher(tmp_path / "log.jsonl")
    gateway = edge.UnavailableGateway()

    result = run_morning_brief_job(_config(), gateway, moment=edge.MORNING, db_path=seeded_db_path, publisher=log, policy=MANUAL)

    assert result.status == GENERATED and gateway.calls == 1
    assert "the model failed for" in result.detail and "shown as recorded" in result.detail
    assert result.delivery_status == PROPOSED and ProposalStore(seeded_db_path).get(result.proposal_id).status == PENDING
    assert log.read_log() == []  # nothing was posted: a person approves it first


def test_the_morning_job_says_nothing_about_failure_when_the_model_answered(seeded_db_path, tmp_path):
    result = run_morning_brief_job(_config(), ScriptedGateway(), moment=edge.MORNING, db_path=seeded_db_path, publisher=LogPublisher(tmp_path / "log.jsonl"), policy=MANUAL)

    assert result.detail == "morning brief generated and grounded"


def test_the_end_of_day_job_with_the_model_unreachable_still_proposes_a_summary_of_the_recorded_changes(seeded_db_path, tmp_path):
    edge.add_item(seeded_db_path, "PM-451", "Ship the export button", "done", [("in_progress", "done", "2026-09-16T06:00:00+00:00")])
    edge.add_item(seeded_db_path, "PM-452", "Wire the audit log", "blocked", [("in_progress", "blocked", "2026-09-16T07:00:00+00:00")])
    edge.add_item(seeded_db_path, "PM-453", "Review the pricing page", "in_review", [("in_progress", "in_review", "2026-09-16T08:00:00+00:00")])
    edge.capture(seeded_db_path, edge.MORNING)
    gateway = edge.UnavailableGateway()

    result = run_end_of_day_job(_config(), gateway, moment=edge.EVENING, db_path=seeded_db_path, publisher=LogPublisher(tmp_path / "log.jsonl"), policy=MANUAL)

    assert result.status == SUMMARISED and gateway.calls == 1  # shipped, pending and newly blocked all have changes: still one call
    assert all(item in result.summary.content for item in ("PM-451", "PM-452", "PM-453", "PM-016"))
    assert edge.unsupported_eod_progress(result.summary.content, edge.capture(seeded_db_path, edge.MORNING), edge.capture(seeded_db_path, edge.EVENING)) == []


# --- the weekly report -------------------------------------------------------------------------------------------------------------------------


FRIDAY = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)


def _weekly_facts(db):
    snaps = tuple(build_current_snapshot(db, taken_at=(FRIDAY - n * weekly_report_job.WEEK).isoformat(), tz_name=TZ) for n in (0, 1, 2))
    return compute_weekly_facts(*snaps)


def test_the_weekly_narrative_with_the_model_unreachable_is_empty_not_an_error_and_every_fact_reads_as_recorded(seeded_db_path):
    facts = _weekly_facts(seeded_db_path)
    gateway = edge.UnavailableGateway()

    narrative = generate_narrative(facts, gateway)

    assert gateway.calls == 1  # the closing sentence is not tried once the model is known to be unavailable
    assert narrative.lines == [] and narrative.closing is None
    assert narrative.dropped and narrative.dropped[0]["reason"] == MODEL_FAILED
    assert all(narrative.text_for(fact).startswith(fact.detail) and "as recorded" in narrative.text_for(fact).lower() for fact in narrative.facts)


def test_a_malformed_closing_sentence_answer_leaves_the_lines_and_drops_only_the_closing(seeded_db_path):
    facts = _weekly_facts(seeded_db_path)

    class LinesThenGarbage:
        def __init__(self):
            self.inner = ScriptedWeeklyGateway()

        def generate(self, prompt, **kwargs):
            if "closing" in prompt.lower() and "sentence" in prompt.lower():
                return edge._reply("not JSON at all")
            return self.inner.generate(prompt, **kwargs)

    narrative = generate_narrative(facts, LinesThenGarbage())

    assert narrative.lines and narrative.closing is None


def test_the_weekly_report_job_with_the_model_unreachable_still_makes_a_report_that_recomputes(seeded_db_path):
    report = weekly_report_job.run_weekly_report_job(FRIDAY, db_path=seeded_db_path, timezone_name=TZ, gateway=edge.UnavailableGateway())

    assert report.problems == [] and report.proposal_id  # the quantities are the same; only the prose is missing
    assert "Completed" in report.text or "completed" in report.text


def test_a_json_answer_of_the_wrong_shape_does_not_break_the_weekly_report(seeded_db_path):
    report = weekly_report_job.run_weekly_report_job(FRIDAY, db_path=seeded_db_path, timezone_name=TZ, gateway=edge.RawGateway(json.dumps({"lines": "all good"})))

    assert report.problems == [] and report.narrative.closing is None
