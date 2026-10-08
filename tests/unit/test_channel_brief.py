"""The channel brief: a morning brief and an end-of-day summary for a REAL Teams channel, built only from real Teams data.

The two real sources are P1's channel outcome record (grounded lines, each with its message) and P1's message store (who said it). Nothing
is generated and the seeded sample project is never read: a fact is in the brief only if a real message, or a real approval of something a real
message said, made it so.

What these tests pin down:

  which record      a morning brief reports the newest record BEFORE today; an evening summary reports TODAY's, which P1 writes after its digest
                    and which it will not read before then; nothing real (none, too old, not cleared, not the contract) means nothing is
                    proposed and the reason is said; a stale brief never passes as current
  who said it       authors come from P1's store and are never guessed: unknown means no author, a placeholder name is no name
  promises          read with the existing commitment rules; "tomorrow" is resolved against the day it was SAID, not the day it was read
  what became work  tracker items and risk entries found by the real message ids the record cites; the sample project never appears
  the message       fixed templates, the same facts make the same message, empty sections say so, long ones say how many more, nothing is reworded
  the gate          a proposal like any other: one per channel per day per kind, approved by a person, posted to the channel only if allowlisted
  the clock         per channel, in its own timezone and on its own working days; the evening waits for P1's digest
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from p1.adapters.teams_publisher_mock import LogPublisher
from spine.approval.proposals import PENDING, ProposalStore

from pm.adapters.risk_log import Risk, RiskLogMock
from pm.adapters.tracker import TrackerItem, TrackerMock
from pm.approval import service
from pm.approval.audit import audit_trail
from pm.approval.proposals import BRIEF_PROPOSAL_TYPE, EOD_PROPOSAL_TYPE
from pm.channel.record import record_file
from pm.channelbrief import facts as f
from pm.channelbrief.facts import (
    EVENING,
    MORNING,
    NoUsableRecord,
    P1Directory,
    compute_channel_brief_facts,
)
from pm.channelbrief.render import (
    LINES_PER_SECTION,
    evidence_lines,
    render_content,
    render_message,
)
from pm.jobs import channel_brief_job as job
from pm.scheduling.channel_briefs import (
    UnknownChannel,
    add_channel_brief_jobs,
    channel_schedule_config,
    resolve_channel,
)

CHANNEL = "19:real-channel@thread.tacv2"
NAME = "real-channel"
SHARON, ESANDU = "aad-sharon", "aad-esandu"
TODAY = date(2026, 10, 8)  # a Thursday


def evidence(message_id: str, text: str, quote=None) -> dict:
    return {"message_id": message_id, "text": text, "quote": quote}


def record_dict(day: str, *, updates=(), blockers=(), decisions=(), questions=(), participation=(), allowlisted=True) -> dict:
    return {
        "schema_version": "1.0", "channel_id": CHANNEL, "channel_display_name": NAME, "date": day, "allowlisted": allowlisted,
        "roster": [SHARON, ESANDU], "generated_at": f"{day}T12:00:00+00:00",
        "updates": list(updates), "blockers": list(blockers), "decisions": list(decisions), "questions": list(questions),
        "participation": list(participation),
    }


@pytest.fixture()
def outcomes(tmp_path):
    root = tmp_path / "outcomes"

    def write(day: str, **kw) -> Path:
        path = record_file(root, CHANNEL, date.fromisoformat(day))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record_dict(day, **kw)), encoding="utf-8")
        return path

    write.root = root
    return write


@pytest.fixture()
def p1(tmp_path) -> P1Directory:
    """P1's message store, as a stand-in: two people, and who posted what, when."""
    path = tmp_path / "p1.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE members (id TEXT PRIMARY KEY, display_name TEXT);"
        "CREATE TABLE messages (id TEXT PRIMARY KEY, author_id TEXT, posted_at TEXT);"
    )
    conn.executemany("INSERT INTO members VALUES (?, ?)", [(SHARON, "Sharon Silva"), (ESANDU, "Esandu O"), ("aad-new", "aad-new")])
    conn.executemany("INSERT INTO messages VALUES (?, ?, ?)", [
        ("m1", SHARON, "2026-10-07T04:00:00.000Z"), ("m2", SHARON, "2026-10-07T05:00:00.000Z"), ("m3", ESANDU, "2026-10-07T06:00:00.000Z"),
        ("m4", "aad-new", "2026-10-07T07:00:00.000Z"),
    ])
    conn.commit()
    conn.close()
    return P1Directory(path)


def facts_for(kind, db, outcomes, p1, day=TODAY):
    return compute_channel_brief_facts(CHANNEL, NAME, kind, day, db_path=db, outcomes_dir=outcomes.root, directory=p1)


# --- which record ------------------------------------------------------------------------------------------------------------------


def test_a_morning_brief_reports_the_newest_record_before_today_not_todays(seeded_db_path, outcomes, p1):
    outcomes("2026-10-06", updates=[evidence("m1", "Older update.")])
    outcomes("2026-10-07", updates=[evidence("m2", "Yesterday's update.")])
    outcomes("2026-10-08", updates=[evidence("m3", "Today's update, which a morning brief must not see.")])

    facts = facts_for(MORNING, seeded_db_path, outcomes, p1)

    assert facts.record_date == "2026-10-07" and [line.text for line in facts.sections["updates"]] == ["Yesterday's update."]
    assert facts.record_age_days == 1


def test_an_evening_summary_reports_todays_record(seeded_db_path, outcomes, p1):
    outcomes("2026-10-07", updates=[evidence("m1", "Yesterday.")])
    outcomes("2026-10-08", updates=[evidence("m2", "Today.")])

    facts = facts_for(EVENING, seeded_db_path, outcomes, p1)

    assert facts.record_date == "2026-10-08" and [line.text for line in facts.sections["updates"]] == ["Today."]


def test_an_evening_summary_waits_for_todays_record_rather_than_reading_an_old_one(seeded_db_path, outcomes, p1):
    outcomes("2026-10-07", updates=[evidence("m1", "Yesterday.")])  # P1 has not written today's yet

    with pytest.raises(NoUsableRecord) as refused:
        facts_for(EVENING, seeded_db_path, outcomes, p1)

    assert refused.value.code == "record_not_written_yet" and "2026-10-07" in refused.value.reason


def test_with_no_record_there_is_nothing_to_report_and_it_says_so(seeded_db_path, outcomes, p1):
    with pytest.raises(NoUsableRecord) as refused:
        facts_for(MORNING, seeded_db_path, outcomes, p1)

    assert refused.value.code == "no_record"


def test_a_record_older_than_a_week_is_not_used(seeded_db_path, outcomes, p1):
    outcomes("2026-09-28", updates=[evidence("m1", "Ten days ago.")])

    with pytest.raises(NoUsableRecord) as refused:
        facts_for(MORNING, seeded_db_path, outcomes, p1)

    assert refused.value.code == "no_record"


def test_a_stale_brief_says_how_old_its_record_is(seeded_db_path, outcomes, p1):
    outcomes("2026-10-05", updates=[evidence("m1", "Monday's update.")])

    content = render_content(facts_for(MORNING, seeded_db_path, outcomes, p1))

    assert "newest record on file, 3 days old: nothing newer has been recorded" in content


@pytest.mark.parametrize("flag", [False, None, "true", "yes", 1])
def test_a_record_p1_was_not_cleared_to_pass_on_is_refused_not_read(seeded_db_path, outcomes, p1, flag):
    path = outcomes("2026-10-07", updates=[evidence("m1", "Should never be read.")])
    data = json.loads(path.read_text())
    data["allowlisted"] = flag
    path.write_text(json.dumps(data))

    with pytest.raises(NoUsableRecord) as refused:
        facts_for(MORNING, seeded_db_path, outcomes, p1)

    assert refused.value.code == "not_allowlisted"


def test_a_file_that_is_not_the_published_contract_is_refused(seeded_db_path, outcomes, p1):
    path = outcomes("2026-10-07")
    path.write_text(json.dumps({"hello": "world"}))

    with pytest.raises(NoUsableRecord) as refused:
        facts_for(MORNING, seeded_db_path, outcomes, p1)

    assert refused.value.code in ("schema_invalid", "not_allowlisted")


# --- who said it ---------------------------------------------------------------------------------------------------------------------


def test_authors_come_from_p1s_store_and_are_never_guessed(seeded_db_path, outcomes, p1):
    outcomes("2026-10-07", updates=[evidence("m1", "Said by Sharon."), evidence("m3", "Said by Esandu."), evidence("m9", "A message P1's store does not know."),
                                    evidence("m4", "Said by someone whose name P1 could not learn.")])

    lines = {line.message_id: line.author for line in facts_for(MORNING, seeded_db_path, outcomes, p1).sections["updates"]}

    assert lines == {"m1": "Sharon Silva", "m3": "Esandu O", "m9": None, "m4": None}  # unknown and placeholder names: no author, not a guess


def test_without_p1s_store_the_brief_still_works_and_names_nobody(seeded_db_path, outcomes, tmp_path):
    outcomes("2026-10-07", updates=[evidence("m1", "An update.")])
    gone = P1Directory(tmp_path / "does_not_exist.db")

    facts = facts_for(MORNING, seeded_db_path, outcomes, gone)

    assert gone.available is False and facts.sections["updates"][0].author is None
    assert "(message m1)" in render_content(facts)


def test_p1s_store_is_opened_read_only(tmp_path):
    path = tmp_path / "p1.db"
    sqlite3.connect(path).executescript("CREATE TABLE members (id TEXT, display_name TEXT); CREATE TABLE messages (id TEXT, author_id TEXT, posted_at TEXT);")
    before = path.read_bytes()

    P1Directory(path)

    assert path.read_bytes() == before


def test_who_had_no_say_comes_from_the_records_participation_and_excluded_is_not_an_absence(seeded_db_path, outcomes, p1):
    outcomes("2026-10-07", participation=[{"member_id": ESANDU, "state": "no_message", "evidence_message_ids": []},
                                          {"member_id": "aad-new", "state": "posted_no_update", "evidence_message_ids": ["m4"]},
                                          {"member_id": SHARON, "state": "excluded", "evidence_message_ids": []}])

    facts = facts_for(MORNING, seeded_db_path, outcomes, p1)

    assert facts.silent == (("Esandu O", "No message"), ("a rostered member", "Posted, no update"))


# --- promises ------------------------------------------------------------------------------------------------------------------------


def test_tomorrow_is_resolved_against_the_day_it_was_said_not_the_day_it_is_read(seeded_db_path, outcomes, p1):
    outcomes("2026-10-07", updates=[evidence("m1", "I'll have the export fix in tomorrow.")])  # said Wed 7 Oct (UTC date of m1)

    (promise,) = facts_for(MORNING, seeded_db_path, outcomes, p1).promises

    assert promise.due_iso == "2026-10-08" and promise.state == "due_today" and promise.line.author == "Sharon Silva" and promise.made_on == "2026-10-07"


def test_promises_are_past_due_due_today_or_upcoming_and_never_marked_done(seeded_db_path, outcomes, p1):
    outcomes("2026-10-07", updates=[evidence("m1", "I'll finish the report by 2026-10-05."), evidence("m2", "We will have the fix in by 2026-10-08."),
                                    evidence("m3", "The panel should land by 2026-10-12."), evidence("m4", "We'll ship the far future thing by 2026-12-25.")])

    promises = facts_for(MORNING, seeded_db_path, outcomes, p1).promises

    assert [(p.due_iso, p.state) for p in promises] == [("2026-10-05", "past_due"), ("2026-10-08", "due_today"), ("2026-10-12", "upcoming")]
    assert "done" not in {p.state for p in promises}  # nothing here can see whether it was done
    assert "nothing here says it was done" in render_content(facts_for(MORNING, seeded_db_path, outcomes, p1))


def test_a_promise_from_an_earlier_record_still_shows_while_it_matters_and_an_undated_one_only_from_the_newest(seeded_db_path, outcomes, p1):
    outcomes("2026-10-05", updates=[evidence("m1", "I'll send the summary by 2026-10-09."), evidence("m2", "I will look into the flaky test.")])
    outcomes("2026-10-07", updates=[evidence("m3", "I will review the panel next.")])

    promises = facts_for(MORNING, seeded_db_path, outcomes, p1).promises

    assert [(p.line.message_id, p.state) for p in promises] == [("m1", "upcoming"), ("m3", "no_date")]  # m2 is undated and from an older record


def test_the_same_promise_said_in_two_records_is_listed_once(seeded_db_path, outcomes, p1):
    line = evidence("m1", "I'll send the summary by 2026-10-09.")
    outcomes("2026-10-06", updates=[line])
    outcomes("2026-10-07", updates=[line])

    assert len(facts_for(MORNING, seeded_db_path, outcomes, p1).promises) == 1


def test_a_status_report_is_not_a_promise(seeded_db_path, outcomes, p1):
    outcomes("2026-10-07", updates=[evidence("m1", "The pagination fix is merged and the backfill came out clean.")])

    assert facts_for(MORNING, seeded_db_path, outcomes, p1).promises == ()


# --- what became work ----------------------------------------------------------------------------------------------------------------


def test_items_and_risks_made_from_the_records_messages_are_found_by_those_message_ids(seeded_db_path, outcomes, p1):
    outcomes("2026-10-07", blockers=[evidence("m1", "The staging key was rotated."), evidence("m2", "Nobody answered the payload question.")])
    TrackerMock(db_path=seeded_db_path).create_item(TrackerItem(id="PM-031", title="The staging key was rotated.", status="blocked",
                                                                sprint_id="sprint-13", created_at="2026-10-07", source_message_id="m1"))
    RiskLogMock(db_path=seeded_db_path).create_risk(Risk(id="RISK-004", title="Nobody answered", description="Waiting. (From real-channel message m2 on 2026-10-07.)",
                                                         severity="high", status="open", opened_at="2026-10-07"))

    work = facts_for(MORNING, seeded_db_path, outcomes, p1).work

    assert [(w.kind, w.ref, w.label, w.message_id) for w in work] == [("tracker", "PM-031", "blocked", "m1"), ("risk", "RISK-004", "high", "m2")]


def test_the_seeded_sample_project_never_appears_in_a_real_channels_brief(seeded_db_path, outcomes, p1):
    outcomes("2026-10-07", blockers=[evidence("m1", "A real blocker.")], updates=[evidence("m2", "A real update.")])

    message = render_message(facts_for(MORNING, seeded_db_path, outcomes, p1))

    for sample in ("Aisha", "Mateo", "Noah", "Olivia", "Wei Chen", "Sofia", "PM-014", "PM-023", "RISK-001", "Sprint 13", "billing sync"):
        assert sample not in message, sample  # the seeded project's people, items and risks have no place here


# --- the message ---------------------------------------------------------------------------------------------------------------------


def test_the_same_facts_always_make_the_same_message(seeded_db_path, outcomes, p1):
    outcomes("2026-10-07", updates=[evidence("m1", "An update.")], blockers=[evidence("m2", "A blocker.")])

    assert render_message(facts_for(MORNING, seeded_db_path, outcomes, p1)) == render_message(facts_for(MORNING, seeded_db_path, outcomes, p1))


def test_lines_are_quoted_exactly_with_their_author_and_message_and_never_reworded(seeded_db_path, outcomes, p1):
    text = "The author attempted to hit the staging mirror, and was rejected: a key was rotated (see #12)."
    outcomes("2026-10-07", blockers=[evidence("m1", text)])

    content = render_content(facts_for(MORNING, seeded_db_path, outcomes, p1))

    assert f"- {text} (Sharon Silva, message m1)" in content


def test_an_empty_channel_gets_an_honest_empty_brief(seeded_db_path, outcomes, p1):
    outcomes("2026-10-07", participation=[{"member_id": SHARON, "state": "no_message", "evidence_message_ids": []}])

    message = render_message(facts_for(MORNING, seeded_db_path, outcomes, p1))

    assert message.count("- none recorded") == 4 and "## No say that day\n- Sharon Silva: no message" in message
    assert "## Said they would" not in message and "## Turned into work" not in message  # nothing to say there, so no heading


def test_a_long_section_is_cut_and_says_how_many_more_there_are(seeded_db_path, outcomes, p1):
    outcomes("2026-10-07", updates=[evidence("m1", f"Update number {n}.") for n in range(1, 9)])

    content = render_content(facts_for(MORNING, seeded_db_path, outcomes, p1))

    assert content.count("Update number") == LINES_PER_SECTION and f"and {8 - LINES_PER_SECTION} more in the record (8 in all)" in content
    assert "## Updates (8)" in content  # the heading counts all of them


def test_the_line_limit_is_a_setting(seeded_db_path, outcomes, p1, monkeypatch):
    monkeypatch.setenv("PM_CHANNEL_BRIEF_LINES", "2")
    outcomes("2026-10-07", updates=[evidence("m1", f"Update number {n}.") for n in range(1, 5)])

    assert render_content(facts_for(MORNING, seeded_db_path, outcomes, p1)).count("Update number") == 2


def test_the_title_and_label_follow_the_kind(seeded_db_path, outcomes, p1):
    outcomes("2026-10-07", updates=[evidence("m1", "An update.")])
    outcomes("2026-10-08", updates=[evidence("m2", "Today.")])

    morning = render_message(facts_for(MORNING, seeded_db_path, outcomes, p1), label="[team] ")
    evening = render_message(facts_for(EVENING, seeded_db_path, outcomes, p1))

    assert morning.startswith("[team] Morning brief — 2026-10-08\n\n") and evening.startswith("End-of-day summary — 2026-10-08\n\n")


def test_every_line_in_the_message_rests_on_a_message_the_record_cites(seeded_db_path, outcomes, p1):
    outcomes("2026-10-07", updates=[evidence("m1", "I'll send it by 2026-10-09."), evidence("m2", "Done.")], blockers=[evidence("m3", "Blocked.")],
             decisions=[evidence("m1", "Agreed.")], questions=[evidence("m4", "Why?")])
    facts = facts_for(MORNING, seeded_db_path, outcomes, p1)
    cited = {"m1", "m2", "m3", "m4"}

    assert {e["reference_id"] for e in evidence_lines(facts)} <= cited
    assert all(any(f"message {m}" in line for m in cited) for line in render_content(facts).splitlines() if line.startswith("- ") and "none recorded" not in line
               and "more in the record" not in line and ": no message" not in line)


# --- the gate: a proposal like any other ---------------------------------------------------------------------------------------------


def make_config(timezone_name="Asia/Colombo", working_days=("Mon", "Tue", "Wed", "Thu", "Fri")):
    from datetime import time

    from pm.scheduling.config import ProjectScheduleConfig

    return ProjectScheduleConfig(channel_id=CHANNEL, timezone=timezone_name, working_days=list(working_days), morning_brief_time=time(8, 0),
                                 end_of_day_time=time(17, 45))


MOMENT = datetime(2026, 10, 8, 2, 30, tzinfo=timezone.utc)  # 08:00 Thursday in Colombo


def run(kind, db, outcomes, p1, **kw):
    return job.run_channel_brief_job(make_config(), NAME, kind, moment=kw.pop("moment", MOMENT), db_path=db, outcomes_dir=outcomes.root, directory=p1,
                                     policy=kw.pop("policy", service.ApprovalPolicy()), **kw)


def test_the_job_proposes_the_brief_for_the_real_channel_and_sends_nothing(seeded_db_path, outcomes, p1, tmp_path):
    outcomes("2026-10-07", blockers=[evidence("m1", "A real blocker.")])
    log = LogPublisher(tmp_path / "out.jsonl")

    result = run(MORNING, seeded_db_path, outcomes, p1, publisher=log)

    proposal = ProposalStore(seeded_db_path).get(result.proposal_id)
    assert result.status == job.PROPOSED_FROM_RECORD and proposal.status == PENDING and proposal.type == BRIEF_PROPOSAL_TYPE
    assert proposal.payload["target_channel"] == CHANNEL and proposal.payload["source"] == "channel_record" and proposal.payload["record_date"] == "2026-10-07"
    assert proposal.payload["content"] == result.message and "A real blocker." in result.message
    assert log.read_log() == []  # proposed, not posted


def test_the_evening_job_makes_an_end_of_day_proposal_from_todays_record(seeded_db_path, outcomes, p1):
    outcomes("2026-10-08", decisions=[evidence("m1", "We agreed to keep the poll at five minutes.")])

    result = run(EVENING, seeded_db_path, outcomes, p1, moment=datetime(2026, 10, 8, 12, 30, tzinfo=timezone.utc))

    proposal = ProposalStore(seeded_db_path).get(result.proposal_id)
    assert proposal.type == EOD_PROPOSAL_TYPE and proposal.payload["record_date"] == "2026-10-08"


def test_a_second_run_finds_the_proposal_the_first_made(seeded_db_path, outcomes, p1):
    outcomes("2026-10-07", updates=[evidence("m1", "An update.")])

    first, second = (run(MORNING, seeded_db_path, outcomes, p1) for _ in range(2))

    assert first.proposal_id == second.proposal_id and second.delivery_status == "already_proposed"
    assert len(ProposalStore(seeded_db_path).list_by_status(PENDING)) == 1


def test_morning_and_evening_for_one_day_are_two_proposals_and_two_channels_are_two(seeded_db_path, outcomes, p1):
    outcomes("2026-10-07", updates=[evidence("m1", "Yesterday.")])
    outcomes("2026-10-08", updates=[evidence("m2", "Today.")])
    morning = run(MORNING, seeded_db_path, outcomes, p1)
    evening = run(EVENING, seeded_db_path, outcomes, p1, moment=datetime(2026, 10, 8, 12, 30, tzinfo=timezone.utc))

    assert morning.proposal_id != evening.proposal_id


def test_with_nothing_real_to_report_from_nothing_is_proposed_and_the_reason_is_said(seeded_db_path, outcomes, p1):
    result = run(MORNING, seeded_db_path, outcomes, p1)

    assert result.status == job.NO_DATA and "no outcome record" in result.detail and result.proposal_id is None
    assert ProposalStore(seeded_db_path).list_by_status(PENDING) == []


def test_a_record_p1_was_not_cleared_to_pass_on_proposes_nothing_and_leaves_the_refusal_on_record(seeded_db_path, outcomes, p1):
    path = outcomes("2026-10-07", updates=[evidence("m1", "Should never be read.")])
    data = json.loads(path.read_text())
    data["allowlisted"] = False
    path.write_text(json.dumps(data))

    result = run(MORNING, seeded_db_path, outcomes, p1)

    assert result.status == job.NO_DATA and ProposalStore(seeded_db_path).list_by_status(PENDING) == []
    conn = sqlite3.connect(seeded_db_path)
    rows = conn.execute("SELECT details FROM audit WHERE action = 'record.refused'").fetchall()
    conn.close()
    assert len(rows) == 1 and json.loads(rows[0][0])["consumer"] == "channel_brief" and json.loads(rows[0][0])["code"] == "not_allowlisted"


def test_a_non_working_day_is_skipped(seeded_db_path, outcomes, p1):
    outcomes("2026-10-02", updates=[evidence("m1", "Friday.")])

    saturday = datetime(2026, 10, 3, 2, 30, tzinfo=timezone.utc)
    result = job.run_channel_brief_job(make_config(), NAME, MORNING, moment=saturday, db_path=seeded_db_path, outcomes_dir=outcomes.root, directory=p1)

    assert result.status == job.SKIPPED_NON_WORKING_DAY


def test_a_channel_can_be_worked_on_a_weekend_when_its_own_config_says_so(seeded_db_path, outcomes, p1):
    outcomes("2026-10-02", updates=[evidence("m1", "Friday.")])
    config = make_config(working_days=("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"))

    result = job.run_channel_brief_job(config, NAME, MORNING, moment=datetime(2026, 10, 3, 2, 30, tzinfo=timezone.utc), db_path=seeded_db_path,
                                       outcomes_dir=outcomes.root, directory=p1, policy=service.ApprovalPolicy())

    assert result.status == job.PROPOSED_FROM_RECORD


def test_a_dry_run_makes_the_message_and_proposes_nothing(seeded_db_path, outcomes, p1):
    outcomes("2026-10-07", updates=[evidence("m1", "An update.")])

    result = run(MORNING, seeded_db_path, outcomes, p1, dry_run=True)

    assert result.message and result.proposal_id is None and ProposalStore(seeded_db_path).list_by_status(PENDING) == []


def test_the_label_setting_prefixes_the_post(seeded_db_path, outcomes, p1, monkeypatch):
    monkeypatch.setenv("PM_CHANNEL_BRIEF_LABEL", "[agent] ")
    outcomes("2026-10-07", updates=[evidence("m1", "An update.")])

    assert run(MORNING, seeded_db_path, outcomes, p1).message.startswith("[agent] Morning brief — 2026-10-08")


def test_the_proposal_keeps_what_was_proposed_with_the_message_behind_every_line(seeded_db_path, outcomes, p1):
    outcomes("2026-10-07", blockers=[evidence("m1", "A blocker.", "quote")], updates=[evidence("m2", "I'll send it by 2026-10-09.")])

    result = run(MORNING, seeded_db_path, outcomes, p1)

    proposal = ProposalStore(seeded_db_path).get(result.proposal_id)
    lines = proposal.original_model_output["lines"]
    assert {l["reference_id"] for l in lines} == {"m1", "m2"} and proposal.source_refs == ("m1", "m2")
    assert proposal.original_model_output["content"] == proposal.payload["content"] and proposal.original_model_output["dropped"] == {}
    assert audit_trail(result.proposal_id, db_path=seeded_db_path).events[0]["details"]["source"] == "channel_record"


def test_approving_posts_exactly_the_proposed_text_to_that_channel_and_only_when_it_is_allowlisted(seeded_db_path, outcomes, p1, tmp_path):
    outcomes("2026-10-07", blockers=[evidence("m1", "A real blocker.")])
    proposal_id = run(MORNING, seeded_db_path, outcomes, p1).proposal_id
    log = LogPublisher(tmp_path / "out.jsonl")
    policy = service.ApprovalPolicy(approver_ids=frozenset({"sharon"}), allowlisted_channel_ids=[CHANNEL])

    result = service.approve_and_send(proposal_id, approver_id="sharon", publisher=log, policy=policy, db_path=seeded_db_path)

    (posted,) = log.read_log()
    assert result.outcome == service.SENT and posted["target"] == CHANNEL and posted["content"] == ProposalStore(seeded_db_path).get(proposal_id).payload["content"]


def test_a_real_publisher_refuses_a_channel_that_is_not_on_the_allowlist(seeded_db_path, outcomes, p1):
    class RealPublisher:
        def post_channel_message(self, channel_id, content):  # not a LogPublisher: a real one is held to the allowlist
            raise AssertionError("must not be reached")

    outcomes("2026-10-07", blockers=[evidence("m1", "A real blocker.")])
    proposal_id = run(MORNING, seeded_db_path, outcomes, p1).proposal_id
    policy = service.ApprovalPolicy(approver_ids=frozenset({"sharon"}), allowlisted_channel_ids=["19:someone-else@thread.tacv2"])

    result = service.approve_and_send(proposal_id, approver_id="sharon", publisher=RealPublisher(), policy=policy, db_path=seeded_db_path)

    assert result.outcome == service.REFUSED and "allowlist" in result.detail
    assert ProposalStore(seeded_db_path).get(proposal_id).status == PENDING


def test_auto_approve_still_waits_for_a_person_to_approve_the_first_brief_for_a_channel(seeded_db_path, outcomes, p1, tmp_path):
    outcomes("2026-10-07", updates=[evidence("m1", "An update.")])
    log = LogPublisher(tmp_path / "out.jsonl")
    policy = service.ApprovalPolicy(approver_ids=frozenset({"sharon"}), auto_approve=True)

    result = run(MORNING, seeded_db_path, outcomes, p1, publisher=log, policy=policy)

    assert result.delivery_status == "proposed" and "waiting for a person" in result.delivery_detail and log.read_log() == []


def test_the_card_for_a_channel_brief_is_the_ordinary_brief_card(seeded_db_path, outcomes, p1):
    from pm.approval import cards

    outcomes("2026-10-07", blockers=[evidence("m1", "A real blocker.")])
    proposal_id = run(MORNING, seeded_db_path, outcomes, p1).proposal_id

    (approval,) = cards.handle_list_pending({}, db_path=seeded_db_path)["approvals"]

    assert approval["proposal_id"] == proposal_id and [a["title"] for a in approval["card"]["actions"]] == ["Approve", "Reject"]
    assert "A real blocker." in json.dumps(approval["card"]) and CHANNEL in json.dumps(approval["card"])


# --- the clock -----------------------------------------------------------------------------------------------------------------------


CONFIG_YAML = """channel_id: "{channel_id}"
display_name: "{name}"
allowlisted: {allowlisted}
roster: ["{sharon}"]
update_window_start: "08:00:00"
update_window_end: "17:30:00"
timezone: "{tz}"
working_days: ["Mon", "Tue", "Wed", "Thu", "Fri"]
non_working_dates: []
length_floor: 10
count_thread_replies: true
ignore_bots: true
daily_digest_time: "{digest}"
weekly_digest_day: "Fri"
weekly_digest_time: "17:45:00"
nudge_enabled: false
nudge_cap_per_day: 1
escalation_threshold_days: 3
channel_owner_id: "owner@example.com"
exceptions: []
version: 1
"""


@pytest.fixture()
def channel_configs(tmp_path):
    directory = tmp_path / "channels"
    directory.mkdir()
    for slug, channel_id, name, tz, digest, allowlisted in (
        ("one", "19:one@thread.tacv2", "Channel-One", "Asia/Colombo", "17:30:00", "true"),
        ("two", "19:two@thread.tacv2", "Channel-Two", "America/New_York", "13:00:00", "true"),
        ("off", "19:off@thread.tacv2", "Not-Allowed", "Asia/Colombo", "17:30:00", "false"),
    ):
        (directory / f"{slug}.yaml").write_text(CONFIG_YAML.format(channel_id=channel_id, name=name, tz=tz, digest=digest, allowlisted=allowlisted, sharon=SHARON))
    return directory


def test_channels_are_found_by_display_name_in_any_case_or_by_id_and_only_if_allowlisted(channel_configs):
    assert resolve_channel("channel-one", channel_configs) == ("19:one@thread.tacv2", "Channel-One")
    assert resolve_channel("19:two@thread.tacv2", channel_configs) == ("19:two@thread.tacv2", "Channel-Two")
    for refused in ("Not-Allowed", "nowhere"):
        with pytest.raises(UnknownChannel):
            resolve_channel(refused, channel_configs)


def test_the_evening_job_waits_for_p1s_digest_in_the_channels_own_timezone(channel_configs):
    one = channel_schedule_config("19:one@thread.tacv2", channel_configs)
    two = channel_schedule_config("19:two@thread.tacv2", channel_configs)

    assert (one.timezone, one.morning_brief_time.isoformat(), one.end_of_day_time.isoformat()) == ("Asia/Colombo", "08:00:00", "17:45:00")
    assert (two.timezone, two.morning_brief_time.isoformat(), two.end_of_day_time.isoformat()) == ("America/New_York", "08:00:00", "13:15:00")


def test_a_morning_and_an_evening_job_are_scheduled_per_channel_at_its_own_time_and_days(channel_configs):
    from apscheduler.schedulers.background import BackgroundScheduler

    scheduler = BackgroundScheduler()
    entries = add_channel_brief_jobs(scheduler, ["Channel-One", "19:two@thread.tacv2"], db_path="unused.db", config_dir=channel_configs)

    jobs = {j.id: j for j in scheduler.get_jobs()}
    assert sorted(jobs) == ["pm:channel_evening:19:one@thread.tacv2", "pm:channel_evening:19:two@thread.tacv2",
                            "pm:channel_morning:19:one@thread.tacv2", "pm:channel_morning:19:two@thread.tacv2"]
    assert [name for _config, name in entries] == ["Channel-One", "Channel-Two"]
    evening_two = jobs["pm:channel_evening:19:two@thread.tacv2"]
    assert str(evening_two.trigger.timezone) == "America/New_York" and "13" in str(evening_two.trigger) and "15" in str(evening_two.trigger)
    assert jobs["pm:channel_morning:19:one@thread.tacv2"].kwargs["kind"] == MORNING and evening_two.kwargs["kind"] == EVENING


def test_a_channel_that_is_not_allowlisted_cannot_be_scheduled(channel_configs):
    from apscheduler.schedulers.background import BackgroundScheduler

    with pytest.raises(UnknownChannel):
        add_channel_brief_jobs(BackgroundScheduler(), ["Not-Allowed"], db_path="unused.db", config_dir=channel_configs)


def test_the_scheduler_script_schedules_only_the_real_channels_unless_asked_for_the_sample_project(channel_configs, monkeypatch, capsys):
    import importlib.util

    spec = importlib.util.spec_from_file_location("run_scheduler_script", Path(__file__).parents[2] / "scripts" / "run_scheduler.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    from pm.scheduling import channel_briefs

    real = channel_briefs.add_channel_brief_jobs
    monkeypatch.setattr(script, "add_channel_brief_jobs", lambda scheduler, channels, **kw: real(scheduler, channels, config_dir=channel_configs, **kw))

    assert script.main(["--print-schedule", "--channel-briefs", "Channel-One,Channel-Two", "--gateway", "scripted"], block=False) == 0
    only_real = capsys.readouterr().out
    assert "Channel Channel-One" in only_real and "Channel Channel-Two" in only_real and "morning_brief" not in only_real and "end_of_day" not in only_real

    assert script.main(["--print-schedule", "--channel-briefs", "Channel-One", "--with-sample-project", "--gateway", "scripted"], block=False) == 0
    assert "morning_brief" in capsys.readouterr().out


def test_the_command_line_makes_the_brief_and_leaves_a_proposal(seeded_db_path, outcomes, p1, channel_configs, monkeypatch, capsys):
    import importlib.util

    spec = importlib.util.spec_from_file_location("run_channel_brief_script", Path(__file__).parents[2] / "scripts" / "run_channel_brief.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    monkeypatch.setattr(script, "resolve_channel", lambda name: (CHANNEL, NAME))
    monkeypatch.setattr(script, "channel_schedule_config", lambda channel_id: make_config())
    monkeypatch.setenv("PM_OUTCOMES_DIR", str(outcomes.root))
    monkeypatch.setattr(f, "P1Directory", lambda *a, **k: p1)
    outcomes("2026-10-07", blockers=[evidence("m1", "A real blocker.")])

    assert script.main(["--channel", NAME, "--at", "2026-10-08T08:00", "--db", str(seeded_db_path)]) == 0

    out = capsys.readouterr().out
    assert "A real blocker." in out and "proposal:" in out and len(ProposalStore(seeded_db_path).list_by_status(PENDING)) == 1
