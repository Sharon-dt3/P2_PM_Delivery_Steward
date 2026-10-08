"""PM-14, golden case 6: approval enforcement.

The row: direct service-layer write attempts against a PENDING and against a
REJECTED proposal must both fail, and the audit record must capture the approver,
the timestamp, the original payload and the applied payload. Acceptance: both
assertions pass.

"A write" is two things: a post to Teams and, since approving a risk-log proposal writes to the
risk log, an entry in the risk log. Both are probed with the same two assertions
(GC6-write-bypass-count / GC6-audit-gap-count for posts, GC6-risk-write-bypass-count /
GC6-risk-audit-gap-count for risk-log writes), and the second half of this file breaks the
risk-log safeguards one at a time.

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

    for metric_id in ("GC6-write-bypass-count", "GC6-audit-gap-count", "GC6-risk-write-bypass-count", "GC6-risk-audit-gap-count"):
        assert results[metric_id].measured == 0 and results[metric_id].passed, results[metric_id].detail


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
    assert {"GC6-write-bypass-count", "GC6-audit-gap-count", "GC6-risk-write-bypass-count", "GC6-risk-audit-gap-count"} <= ids
    assert summary.all_passed


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


# --- the same two assertions for a write to the risk log ------------------------------------------------------


def _risk_bypasses():
    return pm14_cases._measure_risk_bypasses().measured


def _risk_gaps():
    return pm14_cases._measure_risk_audit().measured


def test_the_risk_log_write_is_attacked_pending_and_rejected_many_ways():
    pending = [a for a in pm14_cases.RISK_ATTEMPTS if a.state == "pending"]
    rejected = [a for a in pm14_cases.RISK_ATTEMPTS if a.state == "rejected"]

    assert len(pending) >= 10 and len(rejected) >= 9
    assert len(pm14_cases.RISK_ATTEMPTS) == len({(a.state, a.name) for a in pm14_cases.RISK_ATTEMPTS})
    for needed in ("guarded_send called directly", "send_approved (service)", "not an approver", "store.apply directly", "auto_approve_and_send",
                   "card handler", "_execute"):
        assert any(needed in a.name for a in pending), needed
    for needed in ("re-approved by an authorised approver", "store.approve directly", "payload rewritten after the decision", "card handler"):
        assert any(needed in a.name for a in rejected), needed


def test_every_risk_attempt_ends_in_a_recognised_refusal_not_a_crash():
    detail = pm14_cases._measure_risk_bypasses().detail

    assert "unexpected" not in detail and "pending: 12 of 12" in detail and "rejected: 10 of 10" in detail
    assert "WriteRefusedError" in detail and "IllegalTransitionError" in detail and "outcome refused" in detail and "outcome held" in detail


def test_the_unbroken_system_has_no_risk_bypasses_and_no_risk_audit_gaps():
    assert _risk_bypasses() == 0 and _risk_gaps() == 0


# -- the probe can fail: break each risk-log safeguard, GC6 must notice

def test_it_notices_if_the_write_guard_stops_checking_approval_for_the_risk_log(monkeypatch):
    from pm.approval import service

    monkeypatch.setattr(service, "guarded_send", lambda proposal_id, **kw: kw["send_fn"]())

    assert _risk_bypasses() > 0


def test_it_notices_if_anyone_may_approve_a_risk_entry(monkeypatch):
    from pm.approval import service

    monkeypatch.setattr(service, "_is_approver", lambda approver_id, policy: True)

    assert _risk_bypasses() > 0


def test_it_notices_if_a_rejected_risk_proposal_can_be_approved_again(monkeypatch):
    from spine.approval.proposals import ProposalStore

    monkeypatch.setattr(ProposalStore, "_require_legal", staticmethod(lambda current, to_status: None))

    assert _risk_bypasses() > 0


def test_it_notices_if_a_decided_risk_proposals_payload_can_be_rewritten(monkeypatch):
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

    assert _risk_bypasses() > 0


def test_it_notices_if_auto_approve_may_take_a_risk_entry(monkeypatch):
    from pm.approval import service

    monkeypatch.setattr(service, "MESSAGE_TYPES", service.MESSAGE_TYPES | service.RISK_WRITE_TYPES)  # "a risk entry is a message like any other"

    assert _risk_bypasses() > 0


def test_it_notices_if_a_card_from_nobody_can_write_to_the_risk_log(monkeypatch):
    from pm.approval import cards

    real = cards.handle_card_action

    def trusting(request, *, authenticated_user_id, publisher=None, policy=None, db_path=None):
        return real(request, authenticated_user_id=authenticated_user_id or "gc6.approver", publisher=publisher, policy=policy, db_path=db_path)

    monkeypatch.setattr(pm14_cases, "handle_card_action", trusting)

    assert _risk_bypasses() > 0  # the card with no authenticated user is the attempt that gets through


def test_it_notices_if_the_risk_approver_is_not_recorded(monkeypatch):
    from spine.approval.proposals import ProposalStore

    original = ProposalStore._decide

    def forgetful(self, proposal_id, *, to_status, approver_id, payload=None):
        original(self, proposal_id, to_status=to_status, approver_id="", payload=payload)
        return self.get(proposal_id)

    monkeypatch.setattr(ProposalStore, "_decide", forgetful)

    assert _risk_gaps() > 0


def test_it_notices_if_the_risk_decision_has_no_timestamp(monkeypatch):
    import sqlite3

    from spine.approval.proposals import ProposalStore

    original = ProposalStore._decide

    def undated(self, proposal_id, *, to_status, approver_id, payload=None):
        result = original(self, proposal_id, to_status=to_status, approver_id=approver_id, payload=payload)
        conn = sqlite3.connect(self._db_path)
        conn.execute("UPDATE proposals SET decided_at = NULL WHERE id = ?", (proposal_id,))
        conn.commit()
        conn.close()
        return result

    monkeypatch.setattr(ProposalStore, "_decide", undated)

    assert _risk_gaps() > 0


def test_it_notices_if_the_risk_decisions_are_not_audited(monkeypatch):
    from pm.approval import service

    monkeypatch.setattr(service, "write_audit", lambda *a, **k: None)

    assert _risk_gaps() > 0


def test_it_notices_if_the_severity_the_approver_chose_is_not_recorded(monkeypatch):
    from pm.approval import service

    real = service.write_audit

    def without_severity(db_path, *, actor, action, proposal_id, details=None):
        real(db_path, actor=actor, action=action, proposal_id=proposal_id,
             details={k: v for k, v in (details or {}).items() if k not in ("severity", "severity_source")})

    monkeypatch.setattr(service, "write_audit", without_severity)

    assert _risk_gaps() > 0


def test_it_notices_if_what_was_written_is_not_what_was_approved(monkeypatch):
    from dataclasses import replace

    from pm.approval import risk_apply

    real = risk_apply.plan_writes

    def drifting(proposal, *, risk_log, tracker):
        plan = real(proposal, risk_log=risk_log, tracker=tracker)
        return replace(plan, entries=tuple(e.model_copy(update={"severity": "low"}) for e in plan.entries))

    monkeypatch.setattr(risk_apply, "plan_writes", drifting)

    assert _risk_gaps() > 0


def test_it_notices_if_the_write_log_does_not_record_what_was_written(monkeypatch):
    from pm.approval import service

    real = service.guarded_send

    def vague(proposal_id, **kw):
        return real(proposal_id, **{**kw, "target": "somewhere"})

    monkeypatch.setattr(service, "guarded_send", vague)

    assert _risk_gaps() > 0


def test_it_notices_if_the_runtime_copy_of_the_risk_log_is_not_updated(monkeypatch):
    from pm.approval import risk_apply

    monkeypatch.setattr(risk_apply, "refresh_runtime_copy", lambda risk_log, db_path: "refreshed")

    assert _risk_gaps() > 0


def test_it_notices_if_an_approval_overwrites_what_the_agent_proposed(monkeypatch):
    import sqlite3

    from spine.approval.proposals import ProposalStore

    original = ProposalStore.approve

    def overwriting(self, proposal_id, *, approver_id, payload=None):
        original(self, proposal_id, approver_id=approver_id, payload=payload)
        if payload is not None:
            conn = sqlite3.connect(self._db_path)
            conn.execute("UPDATE proposals SET original_model_output = '{}' WHERE id = ?", (proposal_id,))
            conn.commit()
            conn.close()
        return self.get(proposal_id)

    monkeypatch.setattr(ProposalStore, "approve", overwriting)

    assert _risk_gaps() > 0


def test_it_notices_if_a_rejection_still_writes_something(monkeypatch):
    from pm.adapters.risk_log import Risk
    from pm.approval import service
    from pm.risklog.csv_store import CsvRiskLog, live_risk_log_path

    real = service.reject

    def rejecting_and_writing(proposal_id, **kw):
        result = real(proposal_id, **kw)
        CsvRiskLog(live_risk_log_path()).create_risk(Risk(id="RISK-998", title="leaked", description="x", severity="low", status="open",
                                                          related_item_id=None, opened_at="2026-09-18"))
        return result

    monkeypatch.setattr(service, "reject", rejecting_and_writing)

    assert _risk_gaps() > 0  # the rejected decision is read back from the files and something is there that should not be


# -- the risk probe's own integrity

def test_the_risk_probe_uses_throwaway_files_and_leaves_the_real_risk_log_and_its_setting_alone(monkeypatch):
    import hashlib
    import os
    from pathlib import Path

    repo_log = Path(__file__).resolve().parents[2] / "risk_log" / "risks.csv"
    before = hashlib.md5(repo_log.read_bytes()).hexdigest()
    setting = os.environ.get("PM_RISK_LOG_CSV")

    pm14_cases._measure_risk_bypasses()
    pm14_cases._measure_risk_audit()

    assert hashlib.md5(repo_log.read_bytes()).hexdigest() == before and os.environ.get("PM_RISK_LOG_CSV") == setting


def test_a_crash_in_a_risk_attempt_is_not_counted_as_a_block(monkeypatch):
    def broken(case):
        raise TypeError("a bug in the probe, not a refusal")

    monkeypatch.setattr(pm14_cases, "RISK_ATTEMPTS", [pm14_cases.Attempt("broken attempt", "pending", broken)])

    result = pm14_cases._measure_risk_bypasses()

    assert result.measured == 1 and not result.passed and "unexpected TypeError" in result.detail


def test_a_risk_attempt_that_returns_something_other_than_a_refusal_is_a_failure(monkeypatch):
    monkeypatch.setattr(pm14_cases, "RISK_ATTEMPTS", [pm14_cases.Attempt("silent no-op", "rejected", lambda case: None)])

    result = pm14_cases._measure_risk_bypasses()

    assert result.measured == 1 and "unexpected outcome" in result.detail


def test_a_risk_write_that_lands_is_counted_even_when_the_call_also_raises(monkeypatch):
    from pm.adapters.risk_log import Risk
    from pm.risklog.csv_store import CsvRiskLog

    def writes_then_refuses(case):
        CsvRiskLog(case.world.risk_csv).create_risk(
            Risk(id="RISK-997", title="x", description="x", severity="low", status="open", related_item_id=None, opened_at="2026-09-18"))
        from spine.approval.write_guard import WriteRefusedError

        raise WriteRefusedError("looks refused, but the entry is already in the log")

    monkeypatch.setattr(pm14_cases, "RISK_ATTEMPTS", [pm14_cases.Attempt("writes then raises", "pending", writes_then_refuses)])

    assert pm14_cases._measure_risk_bypasses().measured == 1


# -- the owner and the lead's table, in the audit

def test_it_notices_if_an_owner_the_proposal_evidenced_is_not_written(monkeypatch):
    from pm.approval import risk_apply

    real = risk_apply._batch_entry
    monkeypatch.setattr(risk_apply, "_batch_entry", lambda *a, **k: real(*a, **k).model_copy(update={"owner": None}))

    assert _risk_gaps() > 0


def test_it_notices_if_an_owner_is_invented_for_an_entry_with_none_evidenced(monkeypatch):
    from pm.approval import risk_apply

    real = risk_apply._batch_entry

    def inventing(*a, **k):
        entry = real(*a, **k)
        return entry if entry.owner else entry.model_copy(update={"owner": "Someone (someone)"})

    monkeypatch.setattr(risk_apply, "_batch_entry", inventing)

    assert _risk_gaps() > 0


def test_it_notices_if_the_runtime_copy_of_the_risk_log_loses_the_owner(monkeypatch):
    from pm.risklog.sync import RiskLogSync

    real = RiskLogSync._refresh_runtime
    monkeypatch.setattr(RiskLogSync, "_refresh_runtime",
                        lambda self, risks: real(self, [r.model_copy(update={"owner": None}) for r in risks]))

    assert _risk_gaps() > 0


def test_it_notices_if_the_applied_record_stops_saying_what_happened_to_the_lead_table(monkeypatch):
    from pm.approval import service

    real = service.write_audit

    def without_lead(db_path, *, actor, action, proposal_id, details=None):
        real(db_path, actor=actor, action=action, proposal_id=proposal_id,
             details={k: v for k, v in (details or {}).items() if k != "lead_store"})

    monkeypatch.setattr(service, "write_audit", without_lead)

    assert _risk_gaps() > 0
