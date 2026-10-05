"""The brief names people by their NAME ("Aisha Rahman"), not their tracker id
("aisha.rahman"): it is a message to a team, and it is what gets posted to Teams.

Names are facts, so they come from the tracker's roster in code, never from the
model; the id stays on every fact because grounding and the audit trail use it.
A line that names the WRONG person is a fabrication like any other and must be
rejected, which is why this is tested as hard as the rest of grounding.
"""

from __future__ import annotations

import json

import pytest
from spine.llm.gateway import LLMResponse

from pm.adapters.code_host import Commit
from pm.adapters.risk_log import Risk
from pm.adapters.tracker import Assignee
from pm.eval.pm12_cases import (
    ScriptedGateway,
    _person_block,
    build_seeded_facts,
    count_fabrications,
    measure_gc2,
)
from pm.reporting.facts import compute_morning_brief_facts
from pm.reporting.morning_brief import generate_morning_brief
from pm.state.snapshot import ChannelSnapshot, NormalizedItem, ProjectSnapshot


def _item(item_id, title, assignee, status="done"):
    return NormalizedItem(
        id=item_id, title=title, status=status, raw_status=status, sprint_id="sprint-13",
        assignee_id=assignee, created_at="2026-09-10T00:00:00+00:00",
    )


def _snapshot(roster, items=(), commits=(), risks=()):
    return ProjectSnapshot(
        taken_at="2026-09-16T23:59:59+00:00", items=list(items), commits=list(commits),
        channel=ChannelSnapshot(channel_id="c", messages=[]), risks=list(risks),
        roster=[Assignee(id=i, display_name=n) for i, n in roster],
    )


ROSTER = [("aisha.rahman", "Aisha Rahman"), ("noah.becker", "Noah Becker"), ("sofia.lindqvist", "Sofia Lindqvist")]


class _Says:
    """A model that writes the given text for the first fact it is shown."""

    def __init__(self, text_for):
        self._text_for = text_for

    def generate(self, prompt, **kwargs):
        import re

        facts = re.findall(r"reference_id:\s*(\S+)\n\s*detail:\s*(.+)", prompt)
        lines = [{"text": self._text_for(detail), "reference_id": ref, "quote": detail} for ref, detail in facts]
        return LLMResponse(text=json.dumps({"lines": lines}), provider="fake", model="fake",
                           prompt_tokens=0, completion_tokens=0, latency_ms=0.0, cache_hit=False)


# --- the facts carry names ----------------------------------------------------------------------


def test_each_person_carries_the_name_from_the_roster_and_keeps_the_id():
    facts = compute_morning_brief_facts(_snapshot(ROSTER, [_item("PM-001", "Ship login page", "aisha.rahman")]))

    aisha = next(p for p in facts.people if p.assignee_id == "aisha.rahman")
    assert aisha.name == "Aisha Rahman" and aisha.assignee_id == "aisha.rahman"


def test_someone_not_on_the_roster_is_named_by_their_id():
    facts = compute_morning_brief_facts(_snapshot(ROSTER, [_item("PM-002", "Fix bug", "ghost.user")]))

    ghost = next(p for p in facts.people if p.assignee_id == "ghost.user")
    assert ghost.name == "ghost.user"


def test_two_people_with_the_same_name_are_told_apart():
    roster = [("sam.one", "Sam Lee"), ("sam.two", "Sam Lee"), ("noah.becker", "Noah Becker")]

    facts = compute_morning_brief_facts(_snapshot(roster))

    names = {p.assignee_id: p.name for p in facts.people}
    assert names["sam.one"] == "Sam Lee (sam.one)" and names["sam.two"] == "Sam Lee (sam.two)"
    assert names["noah.becker"] == "Noah Becker"


def test_a_blocker_carries_its_owners_name():
    risk = Risk(id="RISK-001", title="Token refresh failing", description="d", severity="high", status="open",
                related_item_id="PM-001", opened_at="2026-09-10")
    snapshot = _snapshot(ROSTER, [_item("PM-001", "Ship login page", "aisha.rahman", "blocked")], risks=[risk])

    (blocker,) = compute_morning_brief_facts(snapshot).blockers

    assert blocker.assignee_id == "aisha.rahman" and blocker.assignee_name == "Aisha Rahman"


# --- the brief uses them ----------------------------------------------------------------------------------


def _brief(facts, gateway=None):
    return generate_morning_brief(facts, gateway or ScriptedGateway())


def test_headings_and_lines_use_the_name_not_the_id():
    facts = compute_morning_brief_facts(_snapshot(ROSTER, [_item("PM-001", "Ship login page", "aisha.rahman")]))

    content = _brief(facts).content

    assert "## Aisha Rahman" in content and "Aisha Rahman: PM-001 (Ship login page)" in content
    assert "aisha.rahman" not in content


def test_the_silent_person_and_the_commit_only_person_appear_by_name():
    commits = [Commit(sha=f"c{i}", author_id="noah.becker", message="m", committed_at="2026-09-15T10:00:00+00:00") for i in range(3)]
    facts = compute_morning_brief_facts(_snapshot(ROSTER, [_item("PM-001", "Ship login page", "aisha.rahman")], commits=commits))

    content = _brief(facts).content

    assert _person_block(content, "Sofia Lindqvist") == ["- No update: no tracker activity or commits recorded."]
    assert _person_block(content, "Noah Becker") == ["- Commits: 3 recorded; no tracker items."]
    assert "sofia.lindqvist" not in content and "noah.becker" not in content


def test_a_blockers_owner_is_named_in_the_blockers_section():
    risk = Risk(id="RISK-001", title="Token refresh failing", description="d", severity="high", status="open",
                related_item_id="PM-001", opened_at="2026-09-10")
    facts = compute_morning_brief_facts(_snapshot(ROSTER, [_item("PM-001", "Ship login page", "aisha.rahman", "blocked")], risks=[risk]))

    blockers = _brief(facts).content.split("## Blockers")[1]

    assert "Aisha Rahman" in blockers and "aisha.rahman" not in blockers


def test_a_dropped_line_falls_back_to_the_fact_with_the_name(monkeypatch):
    facts = compute_morning_brief_facts(_snapshot(ROSTER, [_item("PM-001", "Ship login page", "aisha.rahman")]))
    bad = {"text": "Totally invented content with 99 numbers.", "reference_id": None}

    class Always:
        def generate(self, prompt, **kwargs):
            return LLMResponse(text=json.dumps({"lines": [bad]}), provider="f", model="f", prompt_tokens=0,
                               completion_tokens=0, latency_ms=0.0, cache_hit=False)

    content = _brief(facts, Always()).content

    assert "- Delivered: Aisha Rahman: PM-001 (Ship login page). [as recorded]" in content


# --- grounding still holds, now for names too -----------------------------------------------------------------


def test_a_line_that_uses_the_id_or_the_name_is_accepted():
    facts = compute_morning_brief_facts(_snapshot(ROSTER, [_item("PM-001", "Ship login page", "aisha.rahman")]))

    for text in ("Aisha Rahman delivered PM-001, the login page.", "aisha.rahman delivered the login page."):
        brief = _brief(facts, _Says(lambda detail, text=text: text))
        assert len(brief.sections["delivered"]) == 1, text


def test_a_line_that_names_the_wrong_person_is_rejected():
    facts = compute_morning_brief_facts(_snapshot(ROSTER, [_item("PM-001", "Ship login page", "aisha.rahman")]))

    brief = _brief(facts, _Says(lambda detail: "Noah Becker delivered the login page."))

    assert brief.sections["delivered"] == []
    assert brief.dropped["delivered"][0]["reason"] == "content_not_supported"
    assert "Noah" in brief.dropped["delivered"][0]["detail"] or "Becker" in brief.dropped["delivered"][0]["detail"]


def test_the_probe_would_flag_a_surviving_line_that_names_the_wrong_person():
    facts = compute_morning_brief_facts(_snapshot(ROSTER, [_item("PM-001", "Ship login page", "aisha.rahman")]))
    brief = _brief(facts)
    line = brief.sections["delivered"][0]
    from spine.grounding.kernel import FactualLine

    forged = FactualLine(**{**line.model_dump(), "text": "Noah Becker: PM-001 (Ship login page)."})
    leaked = brief.model_copy(update={"sections": {**brief.sections, "delivered": [forged]}})

    assert any("Noah" in p or "Becker" in p for p in count_fabrications(leaked, facts))


# --- on the real seeded project ------------------------------------------------------------------------------------


def test_the_seeded_brief_names_everyone_and_shows_no_tracker_ids(seeded_db_path):
    facts = build_seeded_facts(seeded_db_path)

    content = _brief(facts).content

    for name in ("Aisha Rahman", "Mateo Silva", "Noah Becker", "Olivia Dupont", "Olivia Dupree", "Sofia Lindqvist", "Wei Chen"):
        assert f"## {name}" in content, name
    for assignee_id in ("aisha.rahman", "mateo.silva", "noah.becker", "olivia.dupont", "olivia.dupree", "sofia.lindqvist", "wei.chen"):
        assert assignee_id not in content, assignee_id


def test_the_two_similar_olivias_stay_distinct(seeded_db_path):
    content = _brief(build_seeded_facts(seeded_db_path)).content

    assert "## Olivia Dupont" in content and "## Olivia Dupree" in content


def test_gc2_is_still_zero_with_names(seeded_db_path):
    (result,) = measure_gc2(build_seeded_facts(seeded_db_path))

    assert result.measured == 0 and result.passed


@pytest.mark.parametrize("name", ["Aisha Rahman", "Wei Chen"])
def test_the_dashboard_shows_the_brief_with_names(seeded_db_path, monkeypatch, tmp_path, name):
    from datetime import datetime, time, timezone
    from pathlib import Path

    from streamlit.testing.v1 import AppTest

    from pm.approval.service import ApprovalPolicy
    from pm.jobs.morning_brief_job import run_morning_brief_job
    from pm.scheduling.config import ProjectScheduleConfig
    from pm.seed.build import CHANNEL_ID

    monkeypatch.setenv("PM_DB_PATH", str(seeded_db_path))
    monkeypatch.setenv("P1_DB_PATH", str(tmp_path / "no_p1.db"))
    monkeypatch.setenv("PM_APPROVER_IDS", "sharon.silva")
    monkeypatch.setenv("PM_AUTO_APPROVE", "0")
    monkeypatch.setenv("PM_DASHBOARD_USER", "")
    monkeypatch.setenv("TEAMS_PUBLISHER_MODE", "mock")
    monkeypatch.setenv("TEAMS_PUBLISHER_LOG_PATH", str(tmp_path / "log.jsonl"))
    config = ProjectScheduleConfig(
        channel_id=CHANNEL_ID, timezone="Asia/Colombo", working_days=["Mon", "Tue", "Wed", "Thu", "Fri"],
        morning_brief_time=time(8, 0), end_of_day_time=time(17, 0),
    )
    run_morning_brief_job(config, ScriptedGateway(), moment=datetime(2026, 9, 16, 2, 30, tzinfo=timezone.utc),
                          db_path=seeded_db_path, policy=ApprovalPolicy())

    at = AppTest.from_file(str(Path(__file__).resolve().parents[2] / "app" / "approval_dashboard.py"), default_timeout=30).run()

    shown = "\n".join(str(t.value) for t in at.text)
    assert f"## {name}" in shown and "aisha.rahman" not in shown
