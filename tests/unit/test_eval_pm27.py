"""PM-27, golden case 8: a record whose scope/consent flag is not explicitly true is refused outright, with a logged reason, and
produces zero proposals.

The case writes one cleared record and nine kinds of record that must not be used, each carrying the same content (a commitment and
a blocker that name real tracker items), reads every one through both things in this agent that consume a record (PM-26's batches
and PM-24's commitment feed), twice, each against its own database. These tests check three things:

  the analysers   leaks and log mismatches are plain functions of what was observed, tested on hand-made observations
  the scenario    the hand-labelled refusals are exactly what happened, and the cleared control is not refused and does produce output
  the numbers     each guard is broken in turn (a lenient reader like P1's own model, a refusal that is not logged, a reader that
                  refuses everything) and the matching number notices
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from spine.eval.cases import GoldenCaseRegistry
from spine.eval.runner import run_eval

from pm.channel import record as record_module
from pm.channel.record import ChannelRecord, Evidence, Participation
from pm.eval import pm27_cases as gc8
from pm.eval.pm27_cases import Observation
from pm.eval.registrations import register_all

REPO = Path(__file__).resolve().parents[2]
BATCHES, FEED = "channel_batches", "commitment_feed"


def _obs(label="x", code="not_allowlisted", *, proposals=0, writes=0, refusals=(), accepted=(), attempts=2) -> Observation:
    return Observation(label=label, expected_code=code, attempts=attempts, proposals_added=proposals, writes_added=writes,
                       refusals=tuple(refusals), accepted_by=frozenset(accepted))


def _clean_rows(code="not_allowlisted", attempts=2):
    return [(c, code) for c in (BATCHES, FEED) for _ in range(attempts)]


@pytest.fixture(scope="module")
def observations(tmp_path_factory) -> list[Observation]:
    return gc8.run_scenarios(tmp_path_factory.mktemp("gc8"))


# --- the analysers --------------------------------------------------------------------------------------------------------------


def test_a_refused_record_that_produced_anything_is_a_leak():
    clean = [_obs(refusals=_clean_rows())]
    leaked = [_obs(proposals=1, refusals=_clean_rows()), _obs(writes=2, refusals=_clean_rows())]

    assert gc8.leaked_proposals(clean) == 0 and gc8.leaked_writes(clean) == 0
    assert gc8.leaked_proposals(leaked) == 1 and gc8.leaked_writes(leaked) == 2


def test_the_cleared_control_producing_output_is_not_a_leak():
    control = _obs("control", None, proposals=2, writes=1, accepted=(BATCHES, FEED))

    assert gc8.leaked_proposals([control]) == 0 and gc8.leaked_writes([control]) == 0


def test_the_refusal_log_must_hold_exactly_one_row_per_consumer_per_attempt_with_the_expected_code():
    assert gc8.log_mismatches([_obs(refusals=_clean_rows())]) == 0
    assert gc8.log_mismatches([_obs(refusals=[])]) == 4  # nothing logged: four missing
    assert gc8.log_mismatches([_obs(refusals=_clean_rows() + [(BATCHES, "not_allowlisted")])]) == 1  # one row too many
    assert gc8.log_mismatches([_obs(refusals=_clean_rows("schema_invalid"))]) == 8  # the wrong reason: 4 missing, 4 unexpected
    assert gc8.log_mismatches([_obs(refusals=[(BATCHES, "not_allowlisted")] * 2)]) == 2  # only one consumer logged


def test_a_cleared_record_must_log_no_refusal():
    assert gc8.log_mismatches([_obs("control", None, accepted=(BATCHES, FEED))]) == 0
    assert gc8.log_mismatches([_obs("control", None, accepted=(BATCHES, FEED), refusals=[(BATCHES, "not_allowlisted")])]) == 1


def test_a_cleared_record_refused_by_either_consumer_is_a_wrong_refusal():
    assert gc8.wrongly_refused([_obs("control", None, accepted=(BATCHES, FEED))]) == 0
    assert gc8.wrongly_refused([_obs("control", None, accepted=(BATCHES,))]) == 1
    assert gc8.wrongly_refused([_obs("control", None, accepted=())]) == 2
    assert gc8.wrongly_refused([_obs(refusals=_clean_rows())]) == 0  # a record that should be refused being refused is right


def test_a_refused_record_accepted_by_a_consumer_is_counted():
    assert gc8.wrongly_accepted([_obs(refusals=_clean_rows())]) == 0
    assert gc8.wrongly_accepted([_obs(refusals=_clean_rows(), accepted=(FEED,))]) == 1


# --- the scenario against the hand-labelled refusals --------------------------------------------------------------------------------


def test_every_record_that_must_be_refused_is_refused_for_the_hand_labelled_reason(observations):
    wanted = {s.label: s.expected_code for s in gc8.SCENARIOS}

    assert {o.label: o.expected_code for o in observations} == wanted
    assert sum(1 for code in wanted.values() if code) == 11 and sum(1 for code in wanted.values() if code is None) == 2
    for o in observations:
        if o.expected_code:
            assert sorted(o.refusals) == sorted(_clean_rows(o.expected_code, o.attempts)), o.label
            assert not o.accepted_by, o.label


def test_nothing_a_refused_record_holds_reaches_a_proposal_or_the_tracker_or_the_commitments(observations):
    assert gc8.leaked_proposals(observations) == 0 and gc8.leaked_writes(observations) == 0


def test_the_cleared_records_are_not_refused_and_do_produce_output(observations):
    cleared = [o for o in observations if o.expected_code is None]

    assert len(cleared) == 2 and all(o.accepted_by == {BATCHES, FEED} and not o.refusals for o in cleared)
    assert all(o.proposals_added == 2 and o.writes_added == 1 for o in cleared)  # the two batches, and the one commitment


def test_the_scenario_is_the_acceptance_test_a_record_lacking_the_flag_yields_zero_proposals_and_one_logged_refusal(observations):
    lacking = next(o for o in observations if o.label == "flag missing")

    assert lacking.proposals_added == 0 and lacking.writes_added == 0
    assert [r for r in lacking.refusals if r[0] == BATCHES] == [(BATCHES, "not_allowlisted")] * lacking.attempts  # one per read


def test_the_metrics_all_pass_and_none_of_them_is_vacuous(observations):
    results = {m.metric_id: m for m in gc8.metrics(observations)}

    assert all(m.passed for m in results.values()), [m.metric_id for m in results.values() if not m.passed]
    for hard_zero in ("GC8-leaked-proposal-count", "GC8-leaked-write-count", "GC8-accepted-refusable-record-count",
                      "GC8-refusal-log-mismatch-count", "GC8-wrongly-refused-count"):
        assert results[hard_zero].measured == 0
    assert results["GC8-refused-record-count"].measured == 11 and results["GC8-cleared-control-output-count"].measured >= 6


# --- break each guard, and the matching number notices ------------------------------------------------------------------------------


def _lax_load(path):
    """What reading through P1's own pydantic model did: the flag coerced to a bool, so "yes", "true" and 1 pass as cleared."""
    try:
        data = json.loads(Path(path).read_text())
    except ValueError as exc:
        raise record_module.RecordRefused(record_module.NOT_JSON, "not JSON") from exc
    if not isinstance(data, dict) or data.get("schema_version") != "1.0" or any(
            not isinstance(i, dict) or "message_id" not in i for k in ("updates", "blockers", "decisions", "questions") for i in data.get(k, [])):
        raise record_module.RecordRefused(record_module.SCHEMA_INVALID, "not the schema")
    flag = data.get("allowlisted")
    if flag is None or str(flag).lower() in ("false", "0", "no", ""):
        raise record_module.RecordRefused(record_module.NOT_ALLOWLISTED, "the flag is falsy")

    def lines(key, section):
        return tuple(Evidence(i["message_id"], i["text"], i.get("quote"), section) for i in data.get(key, []))

    return ChannelRecord(
        schema_version=data["schema_version"], channel_id=data["channel_id"], channel_display_name=data["channel_display_name"],
        date=data["date"], allowlisted=True, roster=tuple(data["roster"]), generated_at=data["generated_at"], updates=lines("updates", "update"),
        blockers=lines("blockers", "blocker"), decisions=lines("decisions", "decision"), questions=lines("questions", "question"),
        participation=tuple(Participation(p["member_id"], p["state"], ()) for p in data.get("participation", [])),
    )


def test_a_lenient_reader_in_the_commitment_feed_is_caught(tmp_path, monkeypatch):
    from pm.commitments import outcomes

    monkeypatch.setattr(outcomes, "load_record", _lax_load)  # the feed as it was: "yes", "true" and 1 pass as cleared

    broken = gc8.run_scenarios(tmp_path)

    assert gc8.leaked_writes(broken) > 0  # commitments were added from records that were not cleared
    assert gc8.log_mismatches(broken) > 0


def test_a_lenient_reader_in_the_batches_is_caught(tmp_path, monkeypatch):
    from pm.channel import batches

    monkeypatch.setattr(batches, "load_record", _lax_load)

    broken = gc8.run_scenarios(tmp_path)

    assert gc8.leaked_proposals(broken) > 0
    assert gc8.wrongly_accepted(broken) > 0


def test_a_refusal_that_is_not_logged_is_caught(tmp_path, monkeypatch):
    from pm.channel import batches
    from pm.commitments import outcomes

    monkeypatch.setattr(batches, "log_refusal", lambda *a, **k: None)
    monkeypatch.setattr(outcomes, "log_refusal", lambda *a, **k: None)

    broken = gc8.run_scenarios(tmp_path)

    assert gc8.log_mismatches(broken) == 11 * 4  # every refusal, both consumers, both reads
    assert gc8.leaked_proposals(broken) == 0  # nothing leaked: it was refused, just not recorded


def test_a_refusal_logged_under_the_wrong_reason_is_caught(tmp_path, monkeypatch):
    from pm.channel import batches

    original = batches.log_refusal
    monkeypatch.setattr(batches, "log_refusal", lambda db, path, refusal, consumer: original(
        db, path, record_module.RecordRefused("schema_invalid", refusal.reason), consumer=consumer))

    broken = gc8.run_scenarios(tmp_path)

    assert gc8.log_mismatches(broken) > 0


def test_a_reader_that_refuses_everything_is_caught_by_the_control(tmp_path, monkeypatch):
    monkeypatch.setattr(record_module, "_flag_problem", lambda data: "everything is refused")

    broken = gc8.run_scenarios(tmp_path)

    assert gc8.wrongly_refused(broken) == 4  # both cleared records, both consumers
    assert next(m for m in gc8.metrics(broken) if m.metric_id == "GC8-cleared-control-output-count").passed is False


def test_the_scenarios_leave_the_real_database_exactly_as_it_was(tmp_path):
    from pm.storage.db import DEFAULT_DB_PATH

    live = Path(DEFAULT_DB_PATH)
    before = (live.stat().st_size, live.stat().st_mtime_ns) if live.exists() else None

    gc8.run_scenarios(tmp_path)

    assert ((live.stat().st_size, live.stat().st_mtime_ns) if live.exists() else None) == before  # every scenario has a database of its own


# --- registered, printed, recorded ---------------------------------------------------------------------------------------------------


def test_it_is_registered_and_the_numbers_are_recorded(tmp_path):
    registry = GoldenCaseRegistry()
    register_all(registry)
    assert "GC8" in {case.case_id for case in registry.all_cases()}
    results_path = tmp_path / "results.jsonl"

    summary = run_eval(registry, model_id="scripted", results_path=results_path)

    ids = {r.metric_id for r in summary.results}
    assert {"GC8-leaked-proposal-count", "GC8-leaked-write-count", "GC8-accepted-refusable-record-count", "GC8-refusal-log-mismatch-count",
            "GC8-wrongly-refused-count", "GC8-refused-record-count", "GC8-cleared-control-output-count"} <= ids
    assert summary.all_passed
    recorded = {r["metric_id"] for line in results_path.read_text().splitlines() for r in json.loads(line)["results"]}
    assert "GC8-leaked-proposal-count" in recorded


def test_the_eval_script_prints_the_case(monkeypatch, tmp_path, capfd):
    spec = importlib.util.spec_from_file_location("run_eval_script_27", REPO / "scripts" / "run_eval.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "RESULTS_PATH", tmp_path / "results.jsonl")
    monkeypatch.setattr("sys.argv", ["run_eval.py"])

    code = module.main()

    out = capfd.readouterr().out
    assert code == 0 and "Golden case 8" in out and "GC8-leaked-proposal-count" in out
    assert "flag missing" in out and "refused" in out
