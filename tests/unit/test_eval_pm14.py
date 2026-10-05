"""PM-14, golden case 6: approval enforcement.

The row: direct service-layer write attempts against a PENDING and against a
REJECTED proposal must both fail, and the audit record must capture the approver,
the timestamp, the original payload and the applied payload. Acceptance: both
assertions pass.

A zero from an enforcement probe only means something if the probe can fail, so
most of this file breaks each safeguard on purpose and checks that GC6 notices.
"""

from __future__ import annotations

from spine.eval.cases import GoldenCaseRegistry
from spine.eval.runner import run_eval

from pm.eval import pm14_cases
from pm.eval.pm14_cases import ATTEMPTS, measure_gc6
from pm.eval.registrations import register_all


def _by_id(results):
    return {r.metric_id: r for r in results}


# --- the acceptance test -------------------------------------------------------------------------


def test_both_assertions_pass():
    """Writes against pending and rejected proposals all fail, and the audit
    record captures approver, timestamp, original and applied payload."""
    results = _by_id(measure_gc6())

    bypass = results["GC6-write-bypass-count"]
    audit = results["GC6-audit-gap-count"]
    assert bypass.measured == 0 and bypass.passed, bypass.detail
    assert audit.measured == 0 and audit.passed, audit.detail


def test_the_detail_says_what_was_covered():
    results = _by_id(measure_gc6())

    assert "pending:" in results["GC6-write-bypass-count"].detail
    assert "rejected:" in results["GC6-write-bypass-count"].detail
    assert "blocked" in results["GC6-write-bypass-count"].detail
    for field in ("approver", "timestamp", "original", "applied"):
        assert field in results["GC6-audit-gap-count"].detail


def test_it_is_registered_and_runs_through_the_harness(tmp_path):
    registry = GoldenCaseRegistry()
    register_all(registry)
    assert "GC6" in {case.case_id for case in registry.all_cases()}

    summary = run_eval(registry, model_id="scripted", results_path=tmp_path / "r.jsonl")

    ids = {r.metric_id for r in summary.results}
    assert {"GC6-write-bypass-count", "GC6-audit-gap-count"} <= ids and summary.all_passed


# --- the attack catalogue ----------------------------------------------------------------------------


def test_both_states_are_attacked_many_ways():
    pending = [a for a in ATTEMPTS if a.state == "pending"]
    rejected = [a for a in ATTEMPTS if a.state == "rejected"]

    assert len(pending) >= 8 and len(rejected) >= 8
    names = {a.name for a in ATTEMPTS}
    for needed in (
        "guarded_send called directly",
        "send_approved (service)",
        "approve_and_send by someone who is not an approver",
        "store.apply directly",
        "auto_approve_and_send",
        "card handler",
    ):
        assert any(needed in n for n in names), needed
    for needed in ("re-approved by an authorised approver", "store.approve directly", "payload rewritten after the decision"):
        assert any(needed in a.name for a in rejected), needed


def test_every_attempt_has_its_own_fresh_proposal_and_name():
    names = [a.name for a in ATTEMPTS]

    assert len(names) == len({(a.state, a.name) for a in ATTEMPTS})


# --- the probe can fail: break each safeguard, GC6 must notice -----------------------------------------


def _bypasses():
    return _by_id(measure_gc6())["GC6-write-bypass-count"].measured


def _gaps():
    return _by_id(measure_gc6())["GC6-audit-gap-count"].measured


def test_it_notices_if_the_write_guard_stops_checking_approval(monkeypatch):
    from pm.approval import service

    monkeypatch.setattr(service, "guarded_send", lambda proposal_id, **kw: kw["send_fn"]())

    assert _bypasses() > 0


def test_it_notices_if_anyone_may_approve(monkeypatch):
    from pm.approval import service

    monkeypatch.setattr(service, "_is_approver", lambda approver_id, policy: True)

    assert _bypasses() > 0


def test_it_notices_if_a_rejected_proposal_can_be_approved_again(monkeypatch):
    from spine.approval.proposals import ProposalStore

    monkeypatch.setattr(ProposalStore, "_require_legal", staticmethod(lambda current, to_status: None))

    assert _bypasses() > 0


def test_it_notices_if_a_decided_proposals_payload_can_be_rewritten(monkeypatch):
    import json

    from spine.approval import proposals
    from spine.storage.db import get_connection

    def permissive(self, proposal_id, *, payload, source_refs=None, also_if_approved_by=None):
        conn = get_connection(self._db_path)
        conn.execute("UPDATE proposals SET payload = ? WHERE id = ?", (json.dumps(payload), proposal_id))
        conn.commit()
        conn.close()
        return self.get(proposal_id)

    monkeypatch.setattr(proposals.ProposalStore, "refresh_payload", permissive)

    assert _bypasses() > 0


def test_it_notices_if_auto_approve_ignores_its_switch(monkeypatch):
    from pm.approval import service
    from pm.approval.audit import AUTO_APPROVER

    def lax(proposal_id, *, publisher=None, policy=None, store=None, db_path=service.DEFAULT_DB_PATH):
        store = store or service.ProposalStore(db_path)
        store.approve(proposal_id, approver_id=AUTO_APPROVER)  # never looks at policy.auto_approve
        return service._execute(proposal_id, actor=AUTO_APPROVER, publisher=publisher, policy=policy, store=store, db_path=db_path)

    monkeypatch.setattr(service, "auto_approve_and_send", lax)

    assert _bypasses() > 0


def test_it_notices_if_the_approver_is_not_recorded(monkeypatch):
    from spine.approval.proposals import ProposalStore

    original = ProposalStore._decide

    def forgetful(self, proposal_id, *, to_status, approver_id, payload=None):
        original(self, proposal_id, to_status=to_status, approver_id="", payload=payload)
        return self.get(proposal_id)

    monkeypatch.setattr(ProposalStore, "_decide", forgetful)

    assert _gaps() > 0


def test_it_notices_if_the_timestamp_is_not_recorded(monkeypatch):
    from spine.approval.proposals import ProposalStore

    original = ProposalStore._decide

    def undated(self, proposal_id, *, to_status, approver_id, payload=None):
        result = original(self, proposal_id, to_status=to_status, approver_id=approver_id, payload=payload)
        import sqlite3

        conn = sqlite3.connect(self._db_path)
        conn.execute("UPDATE proposals SET decided_at = NULL WHERE id = ?", (proposal_id,))
        conn.commit()
        conn.close()
        return result

    monkeypatch.setattr(ProposalStore, "_decide", undated)

    assert _gaps() > 0


def test_it_notices_if_an_edit_overwrites_the_original(monkeypatch):
    import json
    import sqlite3

    from spine.approval.proposals import ProposalStore

    original = ProposalStore.approve

    def overwriting(self, proposal_id, *, approver_id, payload=None):
        original(self, proposal_id, approver_id=approver_id, payload=payload)
        if payload is not None:
            conn = sqlite3.connect(self._db_path)
            conn.execute("UPDATE proposals SET original_model_output = ? WHERE id = ?", (json.dumps(payload), proposal_id))
            conn.commit()
            conn.close()
        return self.get(proposal_id)

    monkeypatch.setattr(ProposalStore, "approve", overwriting)

    assert _gaps() > 0


def test_it_notices_if_the_applied_payload_is_not_what_was_sent(monkeypatch):
    import json
    import sqlite3

    from spine.approval.proposals import ProposalStore

    original = ProposalStore.apply

    def drifting(self, proposal_id):
        result = original(self, proposal_id)
        conn = sqlite3.connect(self._db_path)
        row = conn.execute("SELECT payload FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
        payload = json.loads(row[0])
        payload["content"] = payload["content"] + " (altered after sending)"
        conn.execute("UPDATE proposals SET payload = ? WHERE id = ?", (json.dumps(payload), proposal_id))
        conn.commit()
        conn.close()
        return result

    monkeypatch.setattr(ProposalStore, "apply", drifting)

    assert _gaps() > 0


def test_it_notices_if_the_approval_decision_is_not_audited(monkeypatch):
    from pm.approval import service

    monkeypatch.setattr(service, "write_audit", lambda *a, **k: None)

    assert _gaps() > 0


def test_the_unbroken_system_has_no_bypasses_and_no_gaps():
    assert _bypasses() == 0 and _gaps() == 0


# --- hygiene ------------------------------------------------------------------------------------------------


def test_the_probe_uses_a_throwaway_database_and_leaves_the_real_one_alone(tmp_path, monkeypatch):
    import hashlib
    from pathlib import Path

    real = Path(__file__).resolve().parents[2] / "data" / "pm.db"
    before = hashlib.md5(real.read_bytes()).hexdigest() if real.exists() else None

    measure_gc6()

    assert (hashlib.md5(real.read_bytes()).hexdigest() if real.exists() else None) == before


def test_attempts_are_not_counted_when_they_fail_by_raising_or_by_refusing():
    """A refusal and an exception both count as 'blocked'; only a write that
    lands counts as a bypass."""
    assert pm14_cases._landed(before=("pending", "{}", None, None), after=("pending", "{}", None, None),
                              sent_rows=0, adapter_calls=0, outbound_lines=0) is False
    assert pm14_cases._landed(before=("pending", "{}", None, None), after=("approved", "{}", "x", "t"),
                              sent_rows=0, adapter_calls=0, outbound_lines=0) is True
    assert pm14_cases._landed(before=("rejected", "{}", "a", "t"), after=("rejected", "{}", "a", "t"),
                              sent_rows=0, adapter_calls=1, outbound_lines=0) is True
    assert pm14_cases._landed(before=("rejected", "{}", "a", "t"), after=("rejected", '{"x":1}', "a", "t"),
                              sent_rows=0, adapter_calls=0, outbound_lines=0) is True
    assert pm14_cases._landed(before=("rejected", "{}", "a", "t"), after=("rejected", "{}", "a", "t"),
                              sent_rows=1, adapter_calls=0, outbound_lines=0) is True


def test_it_is_deterministic():
    first = [(r.metric_id, r.measured, r.detail) for r in measure_gc6()]
    second = [(r.metric_id, r.measured, r.detail) for r in measure_gc6()]

    assert first == second


# --- the probe's own integrity ---------------------------------------------------------------------------------


def test_every_attempt_ends_in_a_recognised_refusal_not_a_crash():
    detail = _by_id(measure_gc6())["GC6-write-bypass-count"].detail

    assert "unexpected" not in detail  # no attempt passed only because the probe itself crashed
    assert "WriteRefusedError" in detail and "IllegalTransitionError" in detail and "outcome refused" in detail


def test_a_crash_in_the_probe_is_not_counted_as_a_block(monkeypatch):
    def broken(case):
        raise TypeError("a bug in the probe, not a refusal")

    monkeypatch.setattr(pm14_cases, "ATTEMPTS", [pm14_cases.Attempt("broken attempt", "pending", broken)])

    result = _by_id(measure_gc6())["GC6-write-bypass-count"]

    assert result.measured == 1 and not result.passed and "unexpected TypeError" in result.detail


def test_an_attempt_that_returns_something_other_than_a_refusal_is_a_failure(monkeypatch):
    monkeypatch.setattr(pm14_cases, "ATTEMPTS", [pm14_cases.Attempt("silent no-op", "pending", lambda case: None)])

    result = _by_id(measure_gc6())["GC6-write-bypass-count"]

    assert result.measured == 1 and "unexpected outcome" in result.detail
