"""PM-14 golden case 6: approval enforcement.

Two assertions, both of which must pass (the row's acceptance test):

1. Direct service-layer write attempts against a PENDING proposal and against a
   REJECTED proposal all fail. Each attempt gets its own fresh proposal and is
   tried the way a careless or hostile caller would: calling the write guard
   directly, calling the service's send, approving without being an approver,
   re-approving something already rejected, poking the proposal store, rewriting a
   decided proposal's payload, going through the Teams card handler, asking
   auto-approve to take it. An attempt "lands" if ANY of these is true afterwards:
   the adapter was called, a line reached the outbound log, a "sent" row exists in
   the write log, or the proposal's status, payload, approver or decision time
   changed. GC6-write-bypass-count is the number that landed: a hard zero.

2. The audit record captures the approver, the timestamp, the original payload
   and the applied payload. Checked for four decided proposals (approved with
   edits by a person, approved as proposed, approved automatically, rejected) by
   reading the raw database rows directly -- never through the application's own
   audit helper, which is only compared against them afterwards -- and against what
   the adapter was actually handed. GC6-audit-gap-count is the number of missing or
   wrong fields: a hard zero.

A zero only means something if the probe can fail, so tests/unit/test_eval_pm14.py
breaks each safeguard in turn and checks that GC6 goes non-zero.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from p1.adapters.teams_publisher_mock import LogPublisher
from spine.approval.proposals import IllegalTransitionError, ProposalStore
from spine.approval.write_guard import WriteRefusedError, guarded_send
from spine.eval.cases import GoldenCase, GoldenCaseRegistry, MetricResult, at_most
from spine.storage.db import run_migrations

from pm.approval import service
from pm.approval.audit import AUTO_APPROVER, audit_trail
from pm.approval.cards import handle_card_action
from pm.approval.proposals import format_brief_message
from pm.approval.service import ApprovalPolicy
from pm.eval.pm12_cases import ScriptedGateway
from pm.jobs.morning_brief_job import run_morning_brief_job
from pm.scheduling.config import ProjectScheduleConfig
from pm.seed.build import CHANNEL_ID, build_seed
from pm.storage.db import MIGRATIONS_DIR, get_connection

APPROVER = "gc6.approver"
POLICY = ApprovalPolicy(approver_ids=frozenset({APPROVER}))
POLICY_AUTO = ApprovalPolicy(
    approver_ids=frozenset({APPROVER}), auto_approve=True, auto_approve_requires_first_human=False
)
MOMENT = datetime(2026, 9, 16, 2, 30, tzinfo=timezone.utc)  # Wed 08:00 in Colombo
TIME_TOLERANCE = timedelta(seconds=5)


class _SpyPublisher(LogPublisher):
    """The adapter: records every call. A LogPublisher, so a send writes nothing
    outside this probe's own temporary directory."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.calls: list[tuple[str, str]] = []

    def post_channel_message(self, channel_id: str, content: str) -> dict:
        self.calls.append((channel_id, content))
        return super().post_channel_message(channel_id, content)

    def outbound_lines(self) -> int:
        return len(self.read_log())


class _World:
    """A throwaway seeded database, the spy adapter, and a way to make a fresh
    proposal for every attempt (each to its own target channel, so the one-per-day
    rule never merges two of them)."""

    def __init__(self, directory: Path) -> None:
        self.db = directory / "gc6.db"
        run_migrations(self.db, MIGRATIONS_DIR)
        conn = get_connection(self.db)
        try:
            build_seed(conn)
        finally:
            conn.close()
        self.spy = _SpyPublisher(directory / "outbound.jsonl")
        self._count = 0

    def propose(self, *, auto_policy: ApprovalPolicy | None = None):
        """Run the real morning job; returns (proposal_id, target_channel, job_result)."""
        self._count += 1
        target = f"19:gc6-{self._count}@thread.tacv2"
        config = ProjectScheduleConfig(
            channel_id=CHANNEL_ID, timezone="Asia/Colombo", working_days=["Mon", "Tue", "Wed", "Thu", "Fri"],
            morning_brief_time=time(8, 0), end_of_day_time=time(17, 0), publish_channel_id=target,
        )
        result = run_morning_brief_job(
            config, ScriptedGateway(), moment=MOMENT, db_path=self.db, publisher=self.spy,
            policy=auto_policy or ApprovalPolicy(),
        )
        return result.proposal_id, target, result

    # raw reads: no application helper in between
    def row(self, proposal_id: str) -> tuple:
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(
                "SELECT status, payload, approver_id, decided_at FROM proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
        finally:
            conn.close()

    def sql(self, query: str, *args) -> list[tuple]:
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(query, args).fetchall()
        finally:
            conn.close()

    def sent_rows(self, proposal_id: str) -> int:
        return self.sql("SELECT count(*) FROM write_log WHERE proposal_id = ? AND status = 'sent'", proposal_id)[0][0]


@dataclass
class _Case:
    world: _World
    proposal_id: str
    target: str

    @property
    def db(self) -> Path:
        return self.world.db

    @property
    def spy(self) -> _SpyPublisher:
        return self.world.spy

    def content(self) -> str:
        return json.loads(self.world.row(self.proposal_id)[1])["content"]


# --- the attempts -------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Attempt:
    name: str
    state: str  # the state the proposal is in when the attempt is made: "pending" or "rejected"
    run: Callable[[_Case], object]


def _guarded_send(c: _Case) -> None:
    guarded_send(
        c.proposal_id, action_type="channel_post", target=c.target,
        send_fn=lambda: c.spy.post_channel_message(c.target, c.content()), db_path=c.db,
    )


def _send_approved(c: _Case):
    return service.send_approved(c.proposal_id, publisher=c.spy, policy=POLICY, db_path=c.db)


def _approve_by(who: str, edited: str | None = None) -> Callable[[_Case], None]:
    def run(c: _Case):
        return service.approve_and_send(
            c.proposal_id, approver_id=who, edited_content=edited, publisher=c.spy, policy=POLICY, db_path=c.db
        )

    return run


def _store(method: str) -> Callable[[_Case], None]:
    def run(c: _Case) -> None:
        store = ProposalStore(c.db)
        if method == "apply":
            store.apply(c.proposal_id)
        else:
            store.approve(c.proposal_id, approver_id=APPROVER)

    return run


def _auto(policy: ApprovalPolicy) -> Callable[[_Case], None]:
    def run(c: _Case):
        return service.auto_approve_and_send(c.proposal_id, publisher=c.spy, policy=policy, db_path=c.db)

    return run


def _card(request_extra: dict, user: str | None) -> Callable[[_Case], None]:
    def run(c: _Case):
        return handle_card_action(
            {"action": "approve", "proposal_id": c.proposal_id, **request_extra},
            authenticated_user_id=user, publisher=c.spy, policy=POLICY, db_path=c.db,
        )

    return run


def _execute_internal(c: _Case):
    store = ProposalStore(c.db)
    return service._execute(c.proposal_id, actor="agent", publisher=c.spy, policy=POLICY, store=store, db_path=c.db)


def _rewrite_payload(c: _Case) -> None:
    store = ProposalStore(c.db)
    payload = json.loads(c.world.row(c.proposal_id)[1])
    store.refresh_payload(c.proposal_id, payload={**payload, "content": "tampered after the decision"})


ATTEMPTS: list[Attempt] = [
    # -- against a PENDING proposal: nobody has approved it
    Attempt("guarded_send called directly", "pending", _guarded_send),
    Attempt("send_approved (service)", "pending", _send_approved),
    Attempt("approve_and_send by someone who is not an approver", "pending", _approve_by("mallory")),
    Attempt("approve_and_send with a blank approver", "pending", _approve_by("  ")),
    Attempt("approve_and_send as the system's own approver id", "pending", _approve_by("system:auto-approve")),
    Attempt("edit-then-approve by someone who is not an approver", "pending", _approve_by("mallory", edited="evil text")),
    Attempt("store.apply directly", "pending", _store("apply")),
    Attempt("auto_approve_and_send with auto-approve off", "pending", _auto(POLICY)),
    Attempt("card handler with no authenticated user", "pending", _card({}, None)),
    Attempt("card handler with a forged approver in the data", "pending", _card({"approver_id": APPROVER}, "mallory")),
    Attempt("service internals (_execute) called directly", "pending", _execute_internal),
    # -- against a REJECTED proposal: a person said no
    Attempt("guarded_send called directly", "rejected", _guarded_send),
    Attempt("send_approved (service)", "rejected", _send_approved),
    Attempt("re-approved by an authorised approver", "rejected", _approve_by(APPROVER)),
    Attempt("edit-then-approve by an authorised approver", "rejected", _approve_by(APPROVER, edited="a different brief")),
    Attempt("store.approve directly", "rejected", _store("approve")),
    Attempt("store.apply directly", "rejected", _store("apply")),
    Attempt("auto_approve_and_send with auto-approve on", "rejected", _auto(POLICY_AUTO)),
    Attempt("card handler approve by an authorised user", "rejected", _card({}, APPROVER)),
    Attempt("service internals (_execute) called directly", "rejected", _execute_internal),
    Attempt("payload rewritten after the decision", "rejected", _rewrite_payload),
]


def _landed(*, before: tuple, after: tuple, sent_rows: int, adapter_calls: int, outbound_lines: int) -> bool:
    """Did the attempt get a write through? Anything that is not a clean refusal:
    the adapter was reached, a line was posted, a send was recorded, or the
    proposal's status, payload, approver or decision time changed."""
    return before != after or sent_rows > 0 or adapter_calls > 0 or outbound_lines > 0


_CLEAN_REFUSAL_EXCEPTIONS = (WriteRefusedError, IllegalTransitionError)
_CLEAN_REFUSAL_OUTCOMES = {"refused", "held"}


def _how_it_ended(attempt: Attempt, case: _Case) -> tuple[bool, str]:
    """(ended in a recognised refusal, label). A crash in the probe itself, or a
    result that is not a refusal, is NOT a pass: the guard has not been shown to
    have blocked anything."""
    try:
        returned = attempt.run(case)
    except _CLEAN_REFUSAL_EXCEPTIONS as exc:
        return True, type(exc).__name__
    except Exception as exc:  # noqa: BLE001 - reported as an unexpected failure below
        return False, f"unexpected {type(exc).__name__}"
    outcome = returned.get("outcome") if isinstance(returned, dict) else getattr(returned, "outcome", None)
    if outcome in _CLEAN_REFUSAL_OUTCOMES:
        return True, f"outcome {outcome}"
    return False, f"unexpected outcome {outcome!r}"


def _try(world: _World, attempt: Attempt) -> tuple[bool, str]:
    """(did the write land or the attempt end unexpectedly, how it ended)."""
    proposal_id, target, _ = world.propose()
    if attempt.state == "rejected":
        service.reject(proposal_id, approver_id=APPROVER, reason="gc6", policy=POLICY, db_path=world.db)
    case = _Case(world, proposal_id, target)
    before = world.row(proposal_id)
    calls_before, lines_before = len(world.spy.calls), world.spy.outbound_lines()
    refused, how = _how_it_ended(attempt, case)
    landed = _landed(
        before=before, after=world.row(proposal_id), sent_rows=world.sent_rows(proposal_id),
        adapter_calls=len(world.spy.calls) - calls_before,
        outbound_lines=world.spy.outbound_lines() - lines_before,
    )
    return landed or not refused, how


def _measure_bypasses(world: _World) -> MetricResult:
    totals = {"pending": [0, 0], "rejected": [0, 0]}  # attempts, failed to be blocked
    how: Counter[str] = Counter()
    first: str | None = None
    for attempt in ATTEMPTS:
        failed, label = _try(world, attempt)
        totals[attempt.state][0] += 1
        totals[attempt.state][1] += failed
        how[label] += 1
        if failed and first is None:
            first = f"{attempt.state}: {attempt.name} ({label})"
    bypassed = sum(failed for _, failed in totals.values())
    detail = "; ".join(f"{s}: {n - b} of {n} write attempts blocked" for s, (n, b) in totals.items())
    detail += "; blocked by " + ", ".join(f"{label} x{count}" for label, count in sorted(how.items()))
    return MetricResult(
        metric_id="GC6-write-bypass-count",
        name="direct write attempts that got through on a pending or rejected proposal (hard zero)",
        measured=bypassed, target=0, comparator_name="at_most", passed=at_most(bypassed, 0),
        detail=detail + (f"; first bypass: {first}" if first else ""),
    )


# --- the audit record ----------------------------------------------------------------------------------------


def _iso(stamp: str | None) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(stamp) if stamp else None
    except ValueError:
        return None
    return parsed if parsed is not None and parsed.tzinfo is not None else None


def _within(stamp: str | None, window: tuple[datetime, datetime]) -> bool:
    moment = _iso(stamp)
    return moment is not None and window[0] - TIME_TOLERANCE <= moment <= window[1] + TIME_TOLERANCE


def _check_decision(
    world: _World, label: str, proposal_id: str, *, approver: str, window: tuple[datetime, datetime],
    original: str, applied: str | None, decision_action: str,
) -> list[str]:
    """Read the raw rows for one decided proposal and report every field that is
    missing or wrong, by field: approver, timestamp, original, applied."""
    gaps: list[str] = []
    status, payload_json, approver_id, decided_at = world.row(proposal_id)
    original_json = world.sql("SELECT original_model_output FROM proposals WHERE id = ?", proposal_id)[0][0]
    payload = json.loads(payload_json)
    events = world.sql(
        "SELECT actor, action, created_at FROM audit WHERE entity_type = 'proposal' AND entity_id = ? ORDER BY id",
        proposal_id,
    )
    decision = next((e for e in events if e[1] == decision_action), None)

    if approver_id != approver:
        gaps.append(f"{label}: approver is {approver_id!r}, expected {approver!r}")
    if decision is None or decision[0] != approver:
        gaps.append(f"{label}: approver not in the audit record ({decision_action})")
    if not _within(decided_at, window):
        gaps.append(f"{label}: timestamp {decided_at!r} missing or outside the decision window")
    if decision is None or not _within(decision[2], window):
        gaps.append(f"{label}: timestamp not in the audit record")
    if json.loads(original_json or "{}").get("content") != original:
        gaps.append(f"{label}: original payload is not what the agent proposed")

    sent = world.sql(
        "SELECT payload FROM write_log WHERE proposal_id = ? AND status = 'sent' ORDER BY id", proposal_id
    )
    if applied is None:  # rejected: nothing may have been applied
        if sent or status == "applied":
            gaps.append(f"{label}: something was applied to a rejected proposal")
        if payload.get("content") != original:
            gaps.append(f"{label}: applied payload changed on a rejected proposal")
    else:
        if status != "applied" or payload.get("content") != applied:
            gaps.append(f"{label}: applied payload is not what was approved")
        logged = json.loads(sent[-1][0]).get("payload", {}).get("content") if sent else None
        if logged != applied:
            gaps.append(f"{label}: the write log does not record the applied payload")
        handed = world.spy.calls[-1][1] if world.spy.calls else None
        if handed != applied:
            gaps.append(f"{label}: the adapter was handed something other than the applied payload")

    trail = audit_trail(proposal_id, db_path=world.db)  # the app's own view must agree with the raw rows
    if (trail.approver_id, trail.decided_at, trail.original_proposal.get("content"), trail.final_proposal.get("content")) != (
        approver_id, decided_at, original, payload.get("content"),
    ):
        gaps.append(f"{label}: the application's audit trail disagrees with the raw rows")
    return gaps


def _measure_audit(world: _World) -> MetricResult:
    gaps: list[str] = []

    # 1. a person edits the agent's text, then approves it
    pid, _, _ = world.propose()
    original = json.loads(world.row(pid)[1])["content"]
    edited = original + "\n\nNote added by the approver."
    before = datetime.now(timezone.utc)
    service.approve_and_send(pid, approver_id=APPROVER, edited_content=edited, publisher=world.spy, policy=POLICY, db_path=world.db)
    gaps += _check_decision(world, "approved with edits", pid, approver=APPROVER, window=(before, datetime.now(timezone.utc)),
                            original=original, applied=edited, decision_action="proposal.approved")

    # 2. a person approves it as proposed
    pid, _, _ = world.propose()
    original = json.loads(world.row(pid)[1])["content"]
    before = datetime.now(timezone.utc)
    service.approve_and_send(pid, approver_id=APPROVER, publisher=world.spy, policy=POLICY, db_path=world.db)
    gaps += _check_decision(world, "approved as proposed", pid, approver=APPROVER, window=(before, datetime.now(timezone.utc)),
                            original=original, applied=original, decision_action="proposal.approved")

    # 3. the system approves it automatically (auto-approve on)
    before = datetime.now(timezone.utc)
    pid, _, result = world.propose(auto_policy=POLICY_AUTO)
    original = format_brief_message("", "2026-09-16", result.brief.content)
    gaps += _check_decision(world, "approved automatically", pid, approver=AUTO_APPROVER, window=(before, datetime.now(timezone.utc)),
                            original=original, applied=original, decision_action="proposal.approved")

    # 4. a person rejects it
    pid, _, _ = world.propose()
    original = json.loads(world.row(pid)[1])["content"]
    before = datetime.now(timezone.utc)
    service.reject(pid, approver_id=APPROVER, reason="gc6", policy=POLICY, db_path=world.db)
    gaps += _check_decision(world, "rejected", pid, approver=APPROVER, window=(before, datetime.now(timezone.utc)),
                            original=original, applied=None, decision_action="proposal.rejected")

    return MetricResult(
        metric_id="GC6-audit-gap-count",
        name="audit-record fields missing or wrong across four decided proposals (hard zero)",
        measured=len(gaps), target=0, comparator_name="at_most", passed=at_most(len(gaps), 0),
        detail=(
            "checked approver, timestamp, original payload and applied payload for: approved with edits, "
            "approved as proposed, approved automatically, rejected"
            + (f"; first gap: {gaps[0]}" if gaps else "")
        ),
    )


def measure_gc6() -> list[MetricResult]:
    with tempfile.TemporaryDirectory(prefix="pm_gc6_") as tmp:
        world = _World(Path(tmp))
        return [_measure_bypasses(world), _measure_audit(world)]


def register(registry: GoldenCaseRegistry) -> None:
    registry.register(
        GoldenCase(
            case_id="GC6",
            description="Approval enforcement: writes on pending/rejected proposals fail; the audit record is complete",
            measure_fn=measure_gc6,
        )
    )
