"""PM-19: blocker -> risk promotion.

A current blocker that is not in the risk log is proposed as a risk once it is OLDER than a
configured number of days. Age comes from the status transitions; the threshold comes from
configuration; the proposal carries drafted mitigation wording and the evidence of how long the
blocker has been open. Arithmetic (age, threshold) in code, prose (description, impact,
mitigation) from the model, held to the evidence.

Acceptance: with the threshold at 2 days, exactly the blockers older than 2 are proposed.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from datetime import datetime, time, timezone
from pathlib import Path

import pytest
from spine.approval.proposals import PENDING, ProposalStore

from pm.approval.service import ApprovalPolicy, reject
from pm.risk.gaps import find_gaps
from pm.risk.promotion import plan_promotion
from pm.risk.promotion_config import (
    PromotionConfigError,
    PromotionPolicy,
    load_promotion_policy,
)
from pm.risk.proposals import RISK_PROPOSAL_TYPE, detect_and_propose
from pm.risk.scripted import ScriptedRiskGateway
from pm.state.snapshot import build_current_snapshot

AS_OF = "2026-09-18T12:00:00+00:00"
TZ = "Asia/Colombo"
TITLE14 = "Search index blocked on staging DB migration"


def _add_blocker(db, item_id, entered_on, *, assignee="olivia.dupree", stored_blocked_since="same", history=None, status="blocked"):
    """A blocker with a hand-set history. `history` overrides the default (in progress, then blocked on `entered_on`)."""
    conn = sqlite3.connect(db)
    stored = entered_on if stored_blocked_since == "same" else stored_blocked_since
    conn.execute(
        "INSERT INTO items (id, title, status, sprint_id, assignee_id, created_at, blocked_since, source_message_id) "
        "VALUES (?, ?, ?, 'sprint-13', ?, '2026-09-01', ?, NULL)",
        (item_id, f"Work item {item_id}", status, assignee, stored),
    )
    rows = history if history is not None else [("backlog", "in_progress", "2026-09-02"), ("in_progress", "blocked", entered_on)]
    for frm, to, at in rows:
        conn.execute("INSERT INTO item_transitions (item_id, from_status, to_status, changed_at) VALUES (?, ?, ?, ?)", (item_id, frm, to, at))
    conn.commit()
    conn.close()


@pytest.fixture()
def db(seeded_db_path):
    """The seeded project plus six more uncovered blockers aged exactly 0, 1, 2, 3, 4 and 10 days on 18 Sep."""
    for item_id, entered in [("PM-101", "2026-09-18"), ("PM-102", "2026-09-17"), ("PM-103", "2026-09-16"),
                             ("PM-104", "2026-09-15"), ("PM-105", "2026-09-14"), ("PM-106", "2026-09-08")]:
        _add_blocker(seeded_db_path, item_id, entered)
    return seeded_db_path


AGES = {"PM-014": 4, "PM-015": 1, "PM-101": 0, "PM-102": 1, "PM-103": 2, "PM-104": 3, "PM-105": 4, "PM-106": 10}  # uncovered blockers


def _snapshot(db, taken_at=AS_OF):
    return build_current_snapshot(db, taken_at=taken_at, tz_name=TZ)


def _policy(days):
    return PromotionPolicy(threshold_days=days, source="test")


def _proposals(db):
    store = ProposalStore(db)
    return [p for s in ("pending", "approved", "rejected", "applied") for p in store.list_by_status(s) if p.type == RISK_PROPOSAL_TYPE]


def _by_item(db):
    return {p.payload["item_id"]: p for p in _proposals(db)}


def _run(db, snapshot=None, policy=None, gateway=None):
    snapshot = snapshot or _snapshot(db)
    return detect_and_propose(snapshot, gateway or ScriptedRiskGateway(find_gaps(snapshot)), db_path=db, promotion=policy)


def _older_than(days):
    return {item for item, age in AGES.items() if age > days}


# --- the acceptance test ---------------------------------------------------------------------------------------------


def test_with_the_threshold_at_two_days_exactly_the_blockers_older_than_two_are_proposed(db):
    """PM-19's acceptance. Ages 4, 3, 4 and 10 are proposed; 2 (exactly), 1, 1 and 0 are not."""
    _run(db, policy=_policy(2))

    assert set(_by_item(db)) == {"PM-014", "PM-104", "PM-105", "PM-106"} == _older_than(2)
    assert all(p.status == PENDING for p in _proposals(db))


def test_a_blocker_exactly_as_old_as_the_threshold_is_not_yet_older(db):
    _run(db, policy=_policy(2))

    assert "PM-103" not in _by_item(db)  # exactly 2 days


def test_on_the_seeded_project_the_threshold_of_two_proposes_the_stale_blocker_only(seeded_db_path):
    """PM-014 has been blocked 4 days, PM-015 only 1. PM-023 and PM-024 are older still but already in the risk log."""
    _run(seeded_db_path, policy=_policy(2))

    assert set(_by_item(seeded_db_path)) == {"PM-014"}


def test_blockers_already_in_the_risk_log_are_never_promoted_however_old(db):
    _run(db, policy=_policy(0))

    text = json.dumps([p.payload for p in _proposals(db)])
    assert "PM-023" not in text and "PM-024" not in text and "RISK-001" not in text  # 8 and 7 days old, but logged


# --- the threshold is configuration -------------------------------------------------------------------------------------


@pytest.mark.parametrize("threshold", range(12))
def test_whatever_the_configured_threshold_exactly_the_older_blockers_are_proposed(db, tmp_path, threshold):
    config = tmp_path / "risk_promotion.yaml"
    config.write_text(f"threshold_days: {threshold}\n")

    _run(db, policy=load_promotion_policy(path=config, env={}))

    assert set(_by_item(db)) == _older_than(threshold)


def test_changing_the_configuration_changes_who_is_proposed(seeded_db_path, tmp_path):
    """Same project, two configuration files: two different answers. A literal in the code could not do this."""
    for item_id, entered in [("PM-104", "2026-09-15"), ("PM-105", "2026-09-14")]:
        _add_blocker(seeded_db_path, item_id, entered)
    two, five = tmp_path / "two.yaml", tmp_path / "five.yaml"
    two.write_text("threshold_days: 2\n")
    five.write_text("threshold_days: 3\n")

    _run(seeded_db_path, policy=load_promotion_policy(path=five, env={}))
    at_three = set(_by_item(seeded_db_path))
    _run(seeded_db_path, policy=load_promotion_policy(path=two, env={}))

    assert at_three == {"PM-014", "PM-105"} and set(_by_item(seeded_db_path)) == {"PM-014", "PM-104", "PM-105"}


def test_the_environment_variable_overrides_the_file(db, tmp_path):
    config = tmp_path / "risk_promotion.yaml"
    config.write_text("threshold_days: 2\n")

    _run(db, policy=load_promotion_policy(path=config, env={"PM_RISK_PROMOTION_THRESHOLD_DAYS": "9"}))

    assert set(_by_item(db)) == {"PM-106"}  # only the 10-day blocker is older than 9


def test_without_a_policy_every_gap_is_proposed_as_in_pm16(db):
    _run(db, policy=None)

    assert set(_by_item(db)) == set(AGES)  # no age requirement
    assert "mitigation" not in _by_item(db)["PM-014"].payload  # and the PM-16 proposal shape is unchanged


# --- the age comes from the transitions ---------------------------------------------------------------------------------


def test_a_blocker_becomes_promotable_as_it_ages(seeded_db_path):
    """PM-015 is 1 day old on the 18th and not proposed; on the 20th it is 3 days old and is."""
    _run(seeded_db_path, policy=_policy(2))
    assert set(_by_item(seeded_db_path)) == {"PM-014"}

    _run(seeded_db_path, snapshot=_snapshot(seeded_db_path, "2026-09-20T12:00:00+00:00"), policy=_policy(2))

    assert set(_by_item(seeded_db_path)) == {"PM-014", "PM-015"}
    assert _by_item(seeded_db_path)["PM-015"].payload["age"]["days"] == 3


def test_a_wrong_stored_blocked_since_does_not_hide_an_old_blocker(seeded_db_path):
    """The tracker's field says PM-014 was blocked today; its transitions say 4 days ago. The transitions win."""
    conn = sqlite3.connect(seeded_db_path)
    conn.execute("UPDATE items SET blocked_since = '2026-09-18' WHERE id = 'PM-014'")
    conn.commit()
    conn.close()

    _run(seeded_db_path, policy=_policy(2))

    age = _by_item(seeded_db_path)["PM-014"].payload["age"]
    assert age["days"] == 4 and age["entered_on"] == "2026-09-14"
    assert age["tracker_blocked_since"] == "2026-09-18" and age["disagrees"] is True
    payload = _by_item(seeded_db_path)["PM-014"].payload
    assert payload["facts"]["days_blocked"] == 4 and payload["facts"]["blocked_since"] == "2026-09-14"  # everything agrees with the transitions
    assert "4 days as of 2026-09-18" in payload["evidence"][0]["text"] and "Open for: 4 days" in payload["content"]


def test_a_flap_resets_the_age_even_if_the_stored_field_says_it_is_old(seeded_db_path):
    """Stored: blocked since 1 Sep. Transitions: blocked, freed, blocked again on 17 Sep: 1 day old, so not promoted at 2."""
    _add_blocker(seeded_db_path, "PM-107", "2026-09-17", stored_blocked_since="2026-09-01", history=[
        ("backlog", "blocked", "2026-09-05"), ("blocked", "in_progress", "2026-09-10"), ("in_progress", "blocked", "2026-09-17")])

    _run(seeded_db_path, policy=_policy(2))
    assert "PM-107" not in _by_item(seeded_db_path)

    _run(seeded_db_path, policy=_policy(0))
    assert _by_item(seeded_db_path)["PM-107"].payload["age"]["days"] == 1


def test_a_blocker_whose_history_does_not_agree_is_not_promoted_and_is_reported(seeded_db_path):
    _add_blocker(seeded_db_path, "PM-108", "2026-09-10", history=[("in_progress", "blocked", "2026-09-10"), ("blocked", "done", "2026-09-12")])
    snapshot = _snapshot(seeded_db_path)

    plan = plan_promotion(snapshot, _policy(0), db_path=seeded_db_path)
    _run(seeded_db_path, policy=_policy(0))

    assert [g.item_id for g, _ in plan.age_unknown] == ["PM-108"]
    assert "PM-108" not in _by_item(seeded_db_path)


# --- the plan ------------------------------------------------------------------------------------------------------------


def test_the_plan_says_who_is_promoted_who_is_not_yet_and_who_is_already_logged(db):
    plan = plan_promotion(_snapshot(db), _policy(2), db_path=db)

    assert {c.gap.item_id: c.age.days for c in plan.eligible} == {"PM-014": 4, "PM-104": 3, "PM-105": 4, "PM-106": 10}
    assert {c.gap.item_id: c.age.days for c in plan.below_threshold} == {"PM-015": 1, "PM-101": 0, "PM-102": 1, "PM-103": 2}
    assert plan.already_logged == ["PM-023", "PM-024"] and plan.policy.threshold_days == 2


def test_the_model_is_only_asked_about_blockers_that_will_be_proposed(db):
    snapshot = _snapshot(db)
    gateway = ScriptedRiskGateway(find_gaps(snapshot))

    _run(db, snapshot, _policy(2), gateway)

    assert gateway.calls == 4  # PM-014, PM-104, PM-105, PM-106: nothing is asked about the younger ones


# --- what a promotion proposal carries --------------------------------------------------------------------------------


def test_the_proposal_carries_the_evidence_of_how_long_it_has_been_open(db):
    _run(db, policy=_policy(2))

    payload = _by_item(db)["PM-014"].payload

    assert payload["age"] == {
        "days": 4, "threshold_days": 2, "entered_on": "2026-09-14", "entered_at": "2026-09-14", "from_status": "in_progress",
        "source": "transition", "as_of": "2026-09-18", "tracker_blocked_since": "2026-09-14", "disagrees": False,
    }
    assert payload["promotion"] == {"threshold_days": 2, "source": "test"}
    refs = [e["ref"] for e in payload["evidence"]]
    assert "transition:PM-014" in refs and "transition:PM-014" in _by_item(db)["PM-014"].source_refs
    assert "in_progress" in next(e["text"] for e in payload["evidence"] if e["ref"] == "transition:PM-014")


def test_the_proposal_carries_drafted_mitigation_wording(db):
    _run(db, policy=_policy(2))

    payload = _by_item(db)["PM-014"].payload

    assert payload["mitigation"] and TITLE14 in payload["mitigation"] and "Olivia Dupree" in payload["mitigation"]
    assert payload["prose_source"] == {"description": "model", "impact": "model", "mitigation": "model"}
    assert _by_item(db)["PM-014"].original_model_output["mitigation"] == payload["mitigation"]


def test_a_person_reading_it_sees_the_age_the_threshold_and_the_mitigation(db):
    _run(db, policy=_policy(2))

    content = _by_item(db)["PM-014"].payload["content"]

    assert "Open for: 4 days" in content and "older than the 2-day threshold" in content
    assert "since 2026-09-14" in content and "Drafted mitigation:" in content and "Blocker reference: item:PM-014" in content


def test_the_mitigation_names_nobody_when_nobody_owns_the_blocker(seeded_db_path):
    _add_blocker(seeded_db_path, "PM-109", "2026-09-10", assignee=None)

    _run(seeded_db_path, policy=_policy(2))

    payload = _by_item(seeded_db_path)["PM-109"].payload
    assert payload["suggested_owner"] is None and "owner" in payload["mitigation"].lower()
    assert "Olivia" not in payload["mitigation"] and "Wei" not in payload["mitigation"]


# --- the model is held to the evidence, mitigation included ----------------------------------------------------------------

MITIGATION_FORGERIES = {
    "a party that is not in the evidence": " Escalate to the CTO.",
    "a person who is not in the evidence": " Escalate to Noah Becker.",
    "a deadline the code did not compute": " Complete within 5 days.",
    "an invented date": " Resolve by 2026-09-30.",
    "an invented plan": " Hire two contractors.",
}


@pytest.mark.parametrize("name", sorted(MITIGATION_FORGERIES))
def test_a_forged_mitigation_never_reaches_a_proposal(db, name):
    suffix = MITIGATION_FORGERIES[name]
    snapshot = _snapshot(db)
    gateway = ScriptedRiskGateway(
        find_gaps(snapshot), persistent=True,
        rewrite=lambda lines, gap: [{**line, "text": line["text"] + suffix} if line["kind"] == "mitigation" else line for line in lines])

    _run(db, snapshot, _policy(2), gateway)

    for proposal in _proposals(db):
        text = json.dumps(proposal.payload)
        for forged in ("CTO", "Noah", "5 days", "2026-09-30", "contractors"):
            assert forged not in text, forged
        assert proposal.payload["prose_source"]["mitigation"] == "template"  # the model's version was refused
        assert proposal.payload["prose_source"]["description"] == "model"  # the honest lines are kept
        assert proposal.original_model_output["dropped"]


def test_plain_management_verbs_are_allowed_in_a_mitigation(db):
    """A mitigation may say what to do with the evidence's own words; it may not add facts."""
    snapshot = _snapshot(db)
    gateway = ScriptedRiskGateway(
        find_gaps(snapshot), persistent=True,
        rewrite=lambda lines, gap: [{**line, "text": f'Escalate and prioritise "{gap.title}".'} if line["kind"] == "mitigation" else line for line in lines])

    _run(db, snapshot, _policy(2), gateway)

    payload = _by_item(db)["PM-014"].payload
    assert payload["mitigation"] == f'Escalate and prioritise "{TITLE14}".' and payload["prose_source"]["mitigation"] == "model"


def test_the_management_verbs_are_for_the_mitigation_only(db):
    """'Escalate and prioritise' is not in the evidence: a description or impact line that uses it is refused."""
    snapshot = _snapshot(db)
    gateway = ScriptedRiskGateway(
        find_gaps(snapshot), persistent=True,
        rewrite=lambda lines, gap: [{**line, "text": line["text"] + " Escalate and prioritise."} if line["kind"] != "mitigation" else line
                                    for line in lines])

    _run(db, snapshot, _policy(2), gateway)

    payload = _by_item(db)["PM-014"].payload
    assert payload["prose_source"] == {"description": "template", "impact": "template", "mitigation": "model"}
    assert "Escalate" not in payload["description"] and "Escalate" not in payload["impact"]


def test_a_mitigation_must_be_anchored_to_its_blocker(db):
    snapshot = _snapshot(db)
    gateway = ScriptedRiskGateway(
        find_gaps(snapshot), persistent=True,
        rewrite=lambda lines, gap: [{**line, "quote": "words that appear nowhere in the evidence"} if line["kind"] == "mitigation" else line for line in lines])

    _run(db, snapshot, _policy(2), gateway)

    assert _by_item(db)["PM-014"].payload["prose_source"]["mitigation"] == "template"


def test_a_model_that_fails_outright_still_gives_a_templated_promotion(db):
    class Broken:
        def generate(self, prompt, **kwargs):
            from spine.llm.gateway import LLMResponse

            return LLMResponse(text="not json", provider="f", model="f", prompt_tokens=0, completion_tokens=0, latency_ms=0.0, cache_hit=False)

    detect_and_propose(_snapshot(db), Broken(), db_path=db, promotion=_policy(2))

    payload = _by_item(db)["PM-014"].payload
    assert payload["prose_source"] == {"description": "template", "impact": "template", "mitigation": "template"}
    assert payload["mitigation"] and payload["age"]["days"] == 4  # the age evidence is code's, so it is still there


def test_the_age_figures_are_codes_whatever_the_model_says(db):
    _run(db, policy=_policy(2))

    for item_id, days in {"PM-014": 4, "PM-104": 3, "PM-105": 4, "PM-106": 10}.items():
        assert _by_item(db)[item_id].payload["age"]["days"] == days == _by_item(db)[item_id].payload["facts"]["days_blocked"]


# --- it rides the same memory and the same gate as every risk proposal -----------------------------------------------------


def test_a_rejected_promotion_is_not_proposed_again(db):
    _run(db, policy=_policy(2))
    reject(_by_item(db)["PM-014"].id, approver_id="sharon.silva", reason="known", policy=ApprovalPolicy(approver_ids=frozenset({"sharon.silva"})),
           db_path=db)
    before = {p.id for p in _proposals(db)}

    results = _run(db, policy=_policy(2))

    assert {p.id for p in _proposals(db)} == before
    assert next(r for r in results if r.item_id == "PM-014").state == "rejected_unchanged"


def test_running_it_again_does_not_duplicate_anything(db):
    _run(db, policy=_policy(2))

    again = _run(db, policy=_policy(2))

    assert len(_proposals(db)) == 4 and not any(r.created for r in again)


def test_promotion_proposals_cannot_be_approved_into_an_action(db):
    from pm.approval import service

    _run(db, policy=_policy(2))
    pid = _by_item(db)["PM-014"].id

    outcome = service.approve_and_send(pid, approver_id="sharon.silva", policy=ApprovalPolicy(approver_ids=frozenset({"sharon.silva"})),
                                       db_path=db)

    assert outcome.outcome == "refused" and ProposalStore(db).get(pid).status == PENDING


# --- the morning job --------------------------------------------------------------------------------------------------------


def _config():
    from pm.scheduling.config import ProjectScheduleConfig
    from pm.seed.build import CHANNEL_ID

    return ProjectScheduleConfig(channel_id=CHANNEL_ID, timezone="Asia/Colombo", working_days=["Mon", "Tue", "Wed", "Thu", "Fri"],
                                 morning_brief_time=time(8, 0), end_of_day_time=time(17, 0))


class _Both:
    def __init__(self, db):
        from pm.eval.pm12_cases import ScriptedGateway

        snap = build_current_snapshot(db, taken_at="2026-09-18T02:30:00+00:00", tz_name=TZ)
        self.brief, self.risk = ScriptedGateway(), ScriptedRiskGateway(find_gaps(snap))

    def generate(self, prompt, **kwargs):
        return (self.risk if "reference_id: item:" in prompt and "risk log entry" in prompt.lower() else self.brief).generate(prompt, **kwargs)


def _job(db):
    from pm.jobs.morning_brief_job import run_morning_brief_job

    return run_morning_brief_job(_config(), _Both(db), moment=datetime(2026, 9, 18, 2, 30, tzinfo=timezone.utc), db_path=db,
                                 policy=ApprovalPolicy())


@pytest.mark.parametrize("threshold,expected", [(2, 1), (0, 2), (5, 0)])
def test_the_morning_job_reads_the_threshold_from_configuration(seeded_db_path, monkeypatch, tmp_path, threshold, expected):
    config = tmp_path / "risk_promotion.yaml"
    config.write_text(f"threshold_days: {threshold}\n")
    monkeypatch.setenv("PM_RISK_DETECTION", "1")
    monkeypatch.setenv("PM_RISK_PROMOTION_CONFIG", str(config))

    result = _job(seeded_db_path)

    assert result.status == "generated" and result.risk_proposals == expected  # PM-014 is 4 days old, PM-015 is 1


def test_a_broken_configuration_never_stops_the_brief_and_proposes_nothing(seeded_db_path, monkeypatch, tmp_path):
    config = tmp_path / "risk_promotion.yaml"
    config.write_text("threshold_days: soon\n")
    monkeypatch.setenv("PM_RISK_DETECTION", "1")
    monkeypatch.setenv("PM_RISK_PROMOTION_CONFIG", str(config))

    result = _job(seeded_db_path)

    assert result.status == "generated" and result.proposal_id and result.risk_proposals == 0
    assert _proposals(seeded_db_path) == []  # an unusable threshold is never replaced by a guess


def test_with_no_configuration_the_job_behaves_as_before(seeded_db_path, monkeypatch, tmp_path):
    monkeypatch.setenv("PM_RISK_DETECTION", "1")
    monkeypatch.setenv("PM_RISK_PROMOTION_CONFIG", str(tmp_path / "none.yaml"))

    assert _job(seeded_db_path).risk_proposals == 2


# --- the command line -------------------------------------------------------------------------------------------------------


@pytest.fixture()
def cli():
    path = Path(__file__).resolve().parents[2] / "scripts" / "detect_risks.py"
    spec = importlib.util.spec_from_file_location("detect_risks_script_19", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cli(cli, db, *args):
    return cli.main(["--db", str(db), "--at", "2026-09-18T12:00", *args])


def test_the_command_shows_the_threshold_the_ages_and_the_decision(cli, seeded_db_path, capsys, tmp_path):
    config = tmp_path / "c.yaml"
    config.write_text("threshold_days: 2\n")

    code = _cli(cli, seeded_db_path, "--promotion-config", str(config), "--dry-run")

    out = capsys.readouterr().out
    assert code == 0 and "threshold: 2 days" in out and str(config) in out
    line14 = next(line for line in out.splitlines() if "PM-014" in line and "days old" in line)
    line15 = next(line for line in out.splitlines() if "PM-015" in line and "day old" in line)
    assert "4 days old" in line14 and "promote" in line14 and "1 day old" in line15 and "not yet" in line15
    assert _proposals(seeded_db_path) == []


def test_the_command_proposes_only_the_older_blocker(cli, seeded_db_path, capsys, tmp_path):
    config = tmp_path / "c.yaml"
    config.write_text("threshold_days: 2\n")

    code = _cli(cli, seeded_db_path, "--promotion-config", str(config), "--gateway", "scripted")

    assert code == 0 and set(_by_item(seeded_db_path)) == {"PM-014"} and "approve.py list" in capsys.readouterr().out


def test_the_threshold_can_be_overridden_for_one_run(cli, seeded_db_path, capsys):
    _cli(cli, seeded_db_path, "--threshold-days", "0", "--gateway", "scripted")

    out = capsys.readouterr().out
    assert set(_by_item(seeded_db_path)) == {"PM-014", "PM-015"} and "command line" in out


def test_the_age_requirement_can_be_switched_off_for_one_run(cli, seeded_db_path):
    _cli(cli, seeded_db_path, "--no-threshold", "--gateway", "scripted")

    assert set(_by_item(seeded_db_path)) == {"PM-014", "PM-015"} and "mitigation" not in _by_item(seeded_db_path)["PM-014"].payload


def test_a_broken_configuration_stops_the_command_with_a_clear_message(cli, seeded_db_path, capsys, tmp_path):
    config = tmp_path / "c.yaml"
    config.write_text("threshold_days: -3\n")

    code = _cli(cli, seeded_db_path, "--promotion-config", str(config))

    out = capsys.readouterr().out
    assert code == 2 and "threshold_days" in out and _proposals(seeded_db_path) == []


def test_load_policy_errors_are_the_documented_type(tmp_path):
    bad = tmp_path / "c.yaml"
    bad.write_text("threshold_days: 1.5\n")

    with pytest.raises(PromotionConfigError):
        load_promotion_policy(path=bad, env={})
