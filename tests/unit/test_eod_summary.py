"""PM-22: the end-of-day summary, built from genuine deltas.

What shipped, what is still pending, what is newly blocked, and what else changed since the
morning, all from the stored diff (pm.state.diff.compute_delta over the morning and end-of-day
snapshots): Python computes the delta and the sections, the model only words each line, held to
the same grounding as the brief. It is never a restatement of the morning brief: only items that
CHANGED are named, each exactly once, however many times it moved.

Acceptance: the summary names exactly the hand-labelled changed set (golden case 3: PM-016, PM-018,
PM-020) and the item that moved twice (PM-016) appears once.
"""

from __future__ import annotations

import re
import sqlite3

import pytest

from pm.adapters.tracker import TrackerMock
from pm.eval.golden_cases import END_OF_DAY, GOLDEN_CASE_3, MORNING
from pm.reporting.end_of_day_facts import (
    NEWLY_BLOCKED,
    OTHER_CHANGES,
    SECTION_ORDER,
    SHIPPED,
    STILL_PENDING,
    compute_end_of_day_facts,
)
from pm.reporting.end_of_day_summary import generate_end_of_day_summary
from pm.reporting.scripted_summary import ScriptedSummaryGateway
from pm.state.diff import compute_delta
from pm.state.snapshot import build_current_snapshot

TZ = "Asia/Colombo"
ID_RE = re.compile(r"\bPM-\d+\b")


def _snapshots(db):
    return (build_current_snapshot(db, taken_at=MORNING, tz_name=TZ), build_current_snapshot(db, taken_at=END_OF_DAY, tz_name=TZ))


def _facts(db, before=None, after=None):
    default_before, default_after = _snapshots(db)
    before, after = before or default_before, after or default_after
    return compute_end_of_day_facts(compute_delta(before, after, TrackerMock(db_path=db)), before, after)


def _summary(db, gateway=None, **kw):
    facts = _facts(db, **kw)
    return facts, generate_end_of_day_summary(facts, gateway or ScriptedSummaryGateway())


def _section(content, title):
    """The lines under one '## title' heading."""
    body = content.split(f"## {title}", 1)[1].split("\n## ", 1)[0]
    return [line for line in body.splitlines() if line.startswith("- ")]


def _add(db, item_id, title, status, history, *, created="2026-09-01", assignee="aisha.rahman"):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO items (id, title, status, sprint_id, assignee_id, created_at, blocked_since, source_message_id) "
        "VALUES (?, ?, ?, 'sprint-13', ?, ?, NULL, NULL)", (item_id, title, status, assignee, created))
    for frm, to, at in history:
        conn.execute("INSERT INTO item_transitions (item_id, from_status, to_status, changed_at) VALUES (?, ?, ?, ?)", (item_id, frm, to, at))
    conn.commit()
    conn.close()


# --- the acceptance test ----------------------------------------------------------------------------------------------


def test_the_summary_names_exactly_the_hand_labelled_changed_set_and_the_twice_moved_item_appears_once(seeded_db_path):
    """PM-22's acceptance. Golden case 3's hand labels: PM-016, PM-018, PM-020 changed; PM-016 moved twice."""
    facts, summary = _summary(seeded_db_path)

    labelled = {label.item_id for label in GOLDEN_CASE_3}
    named = ID_RE.findall(summary.content)
    assert labelled == {"PM-016", "PM-018", "PM-020"}
    assert set(named) == labelled  # exactly the changed set: nothing missing, nothing extra
    assert sorted(named) == sorted(labelled)  # and each is named once, so the item that moved twice is not named twice
    pm016 = next(item for items in facts.sections.values() for item in items if item.item_id == "PM-016")
    assert pm016.transitions_in_window == 2  # it really did move twice (done, then back to in progress)


def test_each_changed_item_is_in_exactly_one_section(seeded_db_path):
    facts = _facts(seeded_db_path)

    placed = [item.item_id for key in SECTION_ORDER for item in facts.sections[key]]
    assert sorted(placed) == ["PM-016", "PM-018", "PM-020"] and len(placed) == len(set(placed))
    assert [i.item_id for i in facts.sections[STILL_PENDING]] == ["PM-018", "PM-020"]  # moved forward, not done
    assert [i.item_id for i in facts.sections[OTHER_CHANGES]] == ["PM-016"]  # churned and ended where it started
    assert facts.sections[SHIPPED] == [] and facts.sections[NEWLY_BLOCKED] == []


def test_the_item_that_moved_twice_shows_its_path(seeded_db_path):
    _, summary = _summary(seeded_db_path)

    (line,) = [ln for ln in summary.content.splitlines() if "PM-016" in ln]
    assert "in_progress -> done -> in_progress" in line and "net status unchanged" in line


# --- what is in each section --------------------------------------------------------------------------------------------


def test_the_summary_has_the_four_sections_in_order(seeded_db_path):
    _, summary = _summary(seeded_db_path)

    headings = re.findall(r"^## (.+)$", summary.content, flags=re.MULTILINE)
    assert headings == ["What shipped", "What is still pending", "What is newly blocked", "What else changed since morning"]
    assert _section(summary.content, "What shipped") == ["- none."]
    assert _section(summary.content, "What is newly blocked") == ["- none."]


@pytest.fixture()
def moves(seeded_db_path):
    """Hand-built changes inside the window, each with a known right answer."""
    day = "2026-09-16T"
    _add(seeded_db_path, "PM-201", "Ship the export button", "done", [("in_progress", "done", day + "11:00:00")])
    _add(seeded_db_path, "PM-202", "Wire the audit log", "blocked", [("in_progress", "blocked", day + "12:00:00")])
    _add(seeded_db_path, "PM-203", "Fix flaky login test", "done", [("in_progress", "blocked", day + "09:00:00"), ("blocked", "done", day + "15:00:00")])
    _add(seeded_db_path, "PM-204", "Review the pricing page", "in_review", [("in_progress", "in_review", day + "10:00:00")])
    _add(seeded_db_path, "PM-205", "Unblock the nightly job", "in_progress", [("blocked", "in_progress", day + "10:30:00")])
    _add(seeded_db_path, "PM-206", "Reopened regression", "in_progress", [("done", "in_progress", day + "13:00:00")])
    _add(seeded_db_path, "PM-207", "Brand new blocked task", "blocked", [("backlog", "blocked", day + "14:00:00")], created=day + "09:30:00")
    _add(seeded_db_path, "PM-208", "Brand new quiet task", "backlog", [], created=day + "09:45:00")
    return seeded_db_path


def test_each_kind_of_change_lands_in_the_right_section(moves):
    facts = _facts(moves)

    where = {item.item_id: key for key in SECTION_ORDER for item in facts.sections[key]}
    assert where["PM-201"] == SHIPPED and where["PM-203"] == SHIPPED  # ended done
    assert where["PM-202"] == NEWLY_BLOCKED and where["PM-207"] == NEWLY_BLOCKED  # ended blocked, was not before
    assert where["PM-204"] == STILL_PENDING and where["PM-205"] == STILL_PENDING and where["PM-206"] == STILL_PENDING
    assert where["PM-208"] == OTHER_CHANGES  # new and quiet
    assert where["PM-016"] == OTHER_CHANGES and where["PM-018"] == STILL_PENDING


def test_an_item_that_moved_twice_and_ended_done_is_shipped_once(moves):
    """Blocked at 09:00 and done at 15:00: one line, under what shipped, with its path; not also under newly blocked."""
    _, summary = _summary(moves)

    assert len(ID_RE.findall(summary.content)) == len(set(ID_RE.findall(summary.content)))  # no item named twice, anywhere
    (line,) = [ln for ln in _section(summary.content, "What shipped") if "PM-203" in ln]
    assert "blocked" in line and "done" in line
    assert not any("PM-203" in ln for ln in _section(summary.content, "What is newly blocked"))


def test_a_reassignment_is_reported_on_the_item_once(seeded_db_path):
    before, after = _snapshots(seeded_db_path)
    moved = after.model_copy(update={"items": [i.model_copy(update={"assignee_id": "wei.chen"}) if i.id == "PM-018" else i for i in after.items]})

    facts, summary = _summary(seeded_db_path, before=before, after=moved)

    assert [i.item_id for i in facts.sections[STILL_PENDING]].count("PM-018") == 1
    (line,) = [ln for ln in summary.content.splitlines() if "PM-018" in ln]
    assert "reassigned" in line


def test_an_item_gone_from_the_tracker_is_an_other_change(seeded_db_path):
    before, after = _snapshots(seeded_db_path)
    gone = after.model_copy(update={"items": [i for i in after.items if i.id != "PM-001"]})

    facts, summary = _summary(seeded_db_path, before=before, after=gone)

    assert "PM-001" in [i.item_id for i in facts.sections[OTHER_CHANGES]]
    assert sum("PM-001" in ln for ln in summary.content.splitlines()) == 1


def test_the_lines_name_the_owner_not_an_id(seeded_db_path):
    facts, summary = _summary(seeded_db_path)

    items = {i.item_id: i for key in SECTION_ORDER for i in facts.sections[key]}
    owned = [i for i in items.values() if i.assignee_id]
    assert owned  # at least one changed item has an owner
    for item in owned:
        assert item.assignee_name and "." not in item.assignee_name and item.assignee_name != item.assignee_id
        assert item.assignee_name in summary.content and item.assignee_id not in summary.content  # the name, never "first.last"
    assert items["PM-018"].assignee_id is None and "Owner: unassigned" in summary.content  # the seed's unowned item says so


# --- never a restatement of the morning brief ----------------------------------------------------------------------------


def test_nothing_that_did_not_change_is_named(seeded_db_path):
    """PM-014 is blocked and PM-015 pending, but nothing happened to them today: they are not in the summary."""
    _, summary = _summary(seeded_db_path)

    for untouched in ("PM-014", "PM-015", "PM-023", "PM-024", "PM-001", "PM-022"):
        assert untouched not in summary.content


def test_none_of_the_briefs_own_material_is_repeated(seeded_db_path):
    """No sprint-scope line, no commitments, no risk entries: those are the morning brief's."""
    _, summary = _summary(seeded_db_path)

    for brief_material in ("RISK-", "committed", "Sprint 13", "sprint-13", "due 2026", "Blockers", "## Aisha", "none."):
        if brief_material == "none.":
            continue  # the empty sections say so
        assert brief_material not in summary.content, brief_material


def test_only_changed_items_are_ever_handed_to_the_model(seeded_db_path):
    prompts = []

    class Spy(ScriptedSummaryGateway):
        def generate(self, prompt, **kwargs):
            prompts.append(prompt)
            return super().generate(prompt, **kwargs)

    _summary(seeded_db_path, Spy())

    shown = set(ID_RE.findall("\n".join(prompts)))
    assert shown == {"PM-016", "PM-018", "PM-020"}  # the prompt contains the delta and nothing else
    assert len(prompts) == 2  # one call per section that has something to say: still pending, other changes


def test_when_nothing_changed_there_is_nothing_to_say_and_no_model_is_asked(seeded_db_path):
    before, _ = _snapshots(seeded_db_path)
    gateway = ScriptedSummaryGateway()

    facts = _facts(seeded_db_path, before=before, after=before)  # a window with nothing in it
    summary = generate_end_of_day_summary(facts, gateway)

    assert gateway.calls == 0 and not ID_RE.findall(summary.content)
    assert all(lines == ["- none."] for lines in (_section(summary.content, t) for t in (
        "What shipped", "What is still pending", "What is newly blocked", "What else changed since morning")))


def test_the_open_items_that_did_not_change_are_counted_not_named(seeded_db_path):
    facts, summary = _summary(seeded_db_path)

    _, after = _snapshots(seeded_db_path)
    unchanged_open = [i for i in after.items if i.status not in ("done", "UNMAPPED") and i.id not in {"PM-016", "PM-018", "PM-020"}]
    assert facts.unchanged_open_count == len(unchanged_open) > 0  # PM-022's status is not one the tracker maps (PM-31): it is not counted as open
    assert f"{len(unchanged_open)} other open items did not change today." in summary.content


# --- the model words it; code decides what it says ----------------------------------------------------------------------------


FORGERIES = {
    "an invented consequence": lambda text: text + " It shipped ahead of schedule.",
    "an invented person": lambda text: text + " Priya reviewed it.",
    "an invented number": lambda text: text + " It took 7 hours.",
    "an invented item": lambda text: text + " It unblocks PM-999.",
}


@pytest.mark.parametrize("persistent", [True, False], ids=["never-corrects", "corrects-on-retry"])
@pytest.mark.parametrize("name", sorted(FORGERIES))
def test_forged_prose_never_reaches_the_summary(seeded_db_path, name, persistent):
    forge = FORGERIES[name]
    gateway = ScriptedSummaryGateway(tamper=lambda lines: [{**l, "text": forge(l["text"])} for l in lines], persistent=persistent)

    _, summary = _summary(seeded_db_path, gateway)

    for forged in ("ahead of schedule", "Priya", "7 hours", "PM-999"):
        assert forged not in summary.content, forged
    assert set(ID_RE.findall(summary.content)) == {"PM-016", "PM-018", "PM-020"}  # every changed item is still there
    if persistent:
        assert summary.content.count("[as recorded]") == 3 and any(summary.dropped.values())  # shown as recorded, not as the model said


@pytest.mark.parametrize("what", ["no reference", "wrong reference", "no quote", "invented quote"])
def test_a_line_that_is_not_anchored_to_its_change_is_dropped(seeded_db_path, what):
    forged = {"no reference": {"reference_id": None}, "wrong reference": {"reference_id": "item:PM-999"},
              "no quote": {"quote": None}, "invented quote": {"quote": "words that are nowhere in the fact"}}[what]
    gateway = ScriptedSummaryGateway(tamper=lambda lines: [{**l, **forged} for l in lines], persistent=True)

    _, summary = _summary(seeded_db_path, gateway)

    assert summary.content.count("[as recorded]") == 3 and any(summary.dropped.values())


def test_a_change_cited_under_the_wrong_section_is_refused(seeded_db_path):
    """The model may not move PM-016 (churned) into the pending section by citing it there."""
    def swap(lines):
        return [{**l, "reference_id": "item:PM-016", "quote": "PM-016 churned"} for l in lines]

    gateway = ScriptedSummaryGateway(tamper=swap, persistent=True)

    _, summary = _summary(seeded_db_path, gateway)

    pending = _section(summary.content, "What is still pending")
    assert all("[as recorded]" in line for line in pending) and not any("churned" in line and "[as recorded]" not in line for line in pending)
    assert summary.dropped["still_pending"]  # the refusal is on record, not just hidden at rendering


def test_a_model_that_repeats_an_item_does_not_make_it_appear_twice(seeded_db_path):
    gateway = ScriptedSummaryGateway(tamper=lambda lines: lines + lines, persistent=True)

    _, summary = _summary(seeded_db_path, gateway)

    assert sorted(ID_RE.findall(summary.content)) == ["PM-016", "PM-018", "PM-020"]


def test_a_model_that_fails_outright_still_gives_the_recorded_changes(seeded_db_path):
    from spine.llm.gateway import LLMResponse

    class Broken:
        calls = 0

        def generate(self, prompt, **kwargs):
            return LLMResponse(text="not json", provider="f", model="f", prompt_tokens=0, completion_tokens=0, latency_ms=0.0, cache_hit=False)

    _, summary = _summary(seeded_db_path, Broken())

    assert set(ID_RE.findall(summary.content)) == {"PM-016", "PM-018", "PM-020"} and summary.content.count("[as recorded]") == 3


def test_the_prompt_used_is_the_versioned_summary_prompt(seeded_db_path):
    prompts = []

    class Spy(ScriptedSummaryGateway):
        def generate(self, prompt, **kwargs):
            prompts.append(prompt)
            return super().generate(prompt, **kwargs)

    _summary(seeded_db_path, Spy())

    assert all("end-of-day summary" in p and "reference_id:" in p for p in prompts)
    assert any('"what is still pending"' in p for p in prompts)


# --- every line says which item it is about, exactly once ------------------------------------------------------------------


def test_a_line_the_model_wrote_without_the_item_id_gets_it_once(seeded_db_path):
    """A model may word a change by its title alone ("The export bug moved..."); the summary still says which item."""
    drop_ids = ScriptedSummaryGateway(tamper=lambda lines: [{**l, "text": re.sub(r"PM-\d+ ", "", l["text"], count=1)} for l in lines], persistent=True)

    _, summary = _summary(seeded_db_path, drop_ids)

    assert sorted(ID_RE.findall(summary.content)) == ["PM-016", "PM-018", "PM-020"]  # named, and still only once each
    assert not summary.dropped["still_pending"]  # the wording itself was accepted: only the id was added
    assert "- PM-018: " in summary.content


def test_a_line_that_already_names_its_item_is_left_alone(seeded_db_path):
    _, summary = _summary(seeded_db_path)

    assert "PM-018: PM-018" not in summary.content and "- PM-018 moved from in_progress to in_review." in summary.content


def test_an_item_blocked_all_day_that_flapped_is_not_newly_blocked(seeded_db_path):
    """Blocked this morning, freed at 10:00, blocked again at 15:00: it ended where it began, so it is churn, not 'newly blocked'."""
    _add(seeded_db_path, "PM-301", "Flaky vendor sandbox", "blocked",
         [("in_progress", "blocked", "2026-09-15T12:00:00"), ("blocked", "in_progress", "2026-09-16T10:00:00"),
          ("in_progress", "blocked", "2026-09-16T15:00:00")])

    facts, _ = _summary(seeded_db_path)

    where = {i.item_id: key for key in SECTION_ORDER for i in facts.sections[key]}
    assert where["PM-301"] == OTHER_CHANGES and not any(i.item_id == "PM-301" for i in facts.sections[NEWLY_BLOCKED])


def test_an_item_that_was_done_all_day_and_flapped_is_not_shipped_today(seeded_db_path):
    """Done yesterday, reopened at 10:00, done again at 15:00: it ended where it began, so nothing shipped."""
    _add(seeded_db_path, "PM-302", "Release notes", "done",
         [("in_progress", "done", "2026-09-15T12:00:00"), ("done", "in_progress", "2026-09-16T10:00:00"), ("in_progress", "done", "2026-09-16T15:00:00")])

    facts, _ = _summary(seeded_db_path)

    where = {i.item_id: key for key in SECTION_ORDER for i in facts.sections[key]}
    assert where["PM-302"] == OTHER_CHANGES and not any(i.item_id == "PM-302" for i in facts.sections[SHIPPED])


def test_a_faithful_line_about_another_section_s_item_is_refused_by_the_section_boundary(seeded_db_path):
    """The wording is exactly right and the quote is real, but the item belongs to another section: only the
    per-section lookup can refuse it, and the refusal must be on record."""
    pm016 = next(i for i in _facts(seeded_db_path).sections[OTHER_CHANGES] if i.item_id == "PM-016")
    gateway = ScriptedSummaryGateway(
        tamper=lambda lines: [{"text": pm016.detail, "reference_id": "item:PM-016", "quote": pm016.detail}], persistent=True)

    _, summary = _summary(seeded_db_path, gateway)

    assert {d["reason"] for d in summary.dropped["still_pending"]} == {"unresolvable_message_id"}
    assert summary.content.count("PM-016") == 1  # still once, in its own section
    assert sorted(ID_RE.findall(summary.content)) == ["PM-016", "PM-018", "PM-020"]
