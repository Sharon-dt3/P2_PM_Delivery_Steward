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

The same two assertions are then made for the other things an approval can do: write to the RISK LOG
(GC6-risk-write-bypass-count, GC6-risk-audit-gap-count) and write to the TRACKER (GC6-tracker-write-bypass-count,
GC6-tracker-audit-gap-count; here the batch is the one made from a channel record, attacked the same ways, and the audit is read back
from the items and comments themselves: what was created, with no assignee and traced to its message, what was commented, what was skipped). A risk-log proposal (here, the risk batch made from a
channel record by the real consumer) is attacked the same ways, pending and rejected, and an attempt lands if the
risk log file changed, the runtime copy of it changed, a "sent" row was logged, anything reached the adapter, or the
proposal changed. For the audit, three decided risk proposals (approved with a severity, approved with none so the
default applies, rejected) are read back from the raw rows and the CSV: approver, timestamp, what the agent proposed,
and what was actually written. Each risk attempt gets its own throwaway database AND its own risk log file.

A zero only means something if the probe can fail, so tests/unit/test_eval_pm14.py
breaks each safeguard in turn and checks that GC6 goes non-zero.
"""

from __future__ import annotations

import csv
import json
import os
import sqlite3
import tempfile
from collections import Counter
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from p1.adapters.teams_publisher_mock import LogPublisher
from spine.approval.proposals import IllegalTransitionError, ProposalStore
from spine.approval.write_guard import WriteRefusedError, guarded_send
from spine.eval.cases import GoldenCase, GoldenCaseRegistry, MetricResult, at_most
from spine.storage.db import run_migrations

from pm.adapters.risk_log import Risk, RiskLogMock
from pm.adapters.tracker import TrackerItem, TrackerMock
from pm.approval import service
from pm.approval.audit import AUTO_APPROVER, audit_trail
from pm.approval.cards import handle_card_action
from pm.approval.proposals import format_brief_message
from pm.approval.service import ApprovalPolicy
from pm.channel.batches import TrackerView, consume
from pm.eval.pm12_cases import ScriptedGateway
from pm.jobs.morning_brief_job import run_morning_brief_job
from pm.risklog.csv_store import CsvRiskLog
from pm.scheduling.config import ProjectScheduleConfig
from pm.seed.build import CHANNEL_ID, RISKS, build_seed
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
            build_seed(conn, risks=RISKS)
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


# --- the same two assertions for a write to the risk log ---------------------------------------------------


RISK_BYPASS_ID = "GC6-risk-write-bypass-count"
RISK_AUDIT_ID = "GC6-risk-audit-gap-count"
TRACKER_BYPASS_ID = "GC6-tracker-write-bypass-count"
TRACKER_AUDIT_ID = "GC6-tracker-audit-gap-count"
RISK_ENTRY_COLUMNS = "id, title, description, severity, status, related_item_id, opened_at, owner"


@contextmanager
def _risk_log_at(path: Path):
    """Real code reads the risk log from PM_RISK_LOG_CSV at call time; point it at the probe's own file and put it back."""
    previous = os.environ.get("PM_RISK_LOG_CSV")
    os.environ["PM_RISK_LOG_CSV"] = str(path)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("PM_RISK_LOG_CSV", None)
        else:
            os.environ["PM_RISK_LOG_CSV"] = previous


class _RiskWorld(_World):
    """A throwaway database AND risk log (the seed's three risks in both), and a way to make a fresh risk-log proposal with the real
    consumer of a channel outcome record: every call is a different record, so no two proposals merge."""

    def __init__(self, directory: Path) -> None:
        super().__init__(directory)
        self.directory = directory
        self.risk_csv = directory / "gc6_risks.csv"
        CsvRiskLog(self.risk_csv).replace_all([Risk(**r) for r in RISKS])
        self.seed_ids = {r["id"] for r in RISKS}
        self._records = 0

    def propose_risk(self, named_item: str | None = None) -> str:
        """A fresh risk-log proposal. With `named_item` the blocker names that tracker item, so the proposal suggests the item's assignee
        as the owner; without, it names none and there is nobody to suggest."""
        self._records += 1
        n = self._records
        record = {
            "schema_version": "1.0", "channel_id": CHANNEL_ID, "channel_display_name": "GC6 channel", "date": "2026-09-18", "allowlisted": True,
            "roster": [], "generated_at": "2026-09-18T11:30:00+00:00", "participation": [], "decisions": [], "questions": [], "updates": [],
            "blockers": [{"message_id": f"gc6-m{n}", "quote": None, "text": (
                f"{named_item} is blocked on vendor feed number {n}." if named_item else f"The vendor feed number {n} has no documented schema.")}],
        }
        path = self.directory / f"record_{n}.json"
        path.write_text(json.dumps(record), encoding="utf-8")
        view = TrackerView.from_adapters(TrackerMock(db_path=self.db), RiskLogMock(db_path=self.db))
        return consume(path, view, db_path=self.db).risk.proposal_id

    def original_items(self, proposal_id: str) -> list[dict]:
        return json.loads(self.sql("SELECT original_model_output FROM proposals WHERE id = ?", proposal_id)[0][0]).get("items", [])

    # raw reads of both copies of the log: the file as bytes and rows, the runtime table as rows
    def csv_rows(self) -> list[dict]:
        with open(self.risk_csv, newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    def runtime_rows(self) -> list[tuple]:
        return self.sql(f"SELECT {RISK_ENTRY_COLUMNS} FROM risks ORDER BY id")

    def tracker_rows(self) -> tuple:
        return (tuple(self.sql("SELECT * FROM items ORDER BY id")), tuple(self.sql("SELECT * FROM item_comments ORDER BY id")))

    def state(self, proposal_id: str) -> tuple:
        return (self.row(proposal_id), self.risk_csv.read_bytes(), tuple(self.runtime_rows()), self.tracker_rows())

    def propose_tracker(self, comment_text: str | None = None) -> str:
        """A fresh tracker-changes proposal from the real consumer: one comment on PM-016 and one new item (a blocker naming none)."""
        self._records += 1
        n = self._records
        self.last_comment_text = comment_text or f"PM-016 note number {n} was reviewed today."
        record = {
            "schema_version": "1.0", "channel_id": CHANNEL_ID, "channel_display_name": "GC6 channel", "date": "2026-09-18", "allowlisted": True,
            "roster": [], "generated_at": "2026-09-18T11:30:00+00:00", "participation": [], "decisions": [], "questions": [],
            "updates": [{"message_id": f"gc6-u{n}", "text": self.last_comment_text, "quote": None}],
            "blockers": [{"message_id": f"gc6-t{n}", "text": f"The vendor feed number {n} has no documented schema.", "quote": None}],
        }
        path = self.directory / f"record_{n}.json"
        path.write_text(json.dumps(record), encoding="utf-8")
        view = TrackerView.from_adapters(TrackerMock(db_path=self.db), RiskLogMock(db_path=self.db))
        return consume(path, view, db_path=self.db).tracker.proposal_id


@dataclass
class _RiskCase:
    world: _RiskWorld
    proposal_id: str
    target: str = ""

    @property
    def db(self) -> Path:
        return self.world.db

    @property
    def spy(self) -> _SpyPublisher:
        return self.world.spy


def _risk_guarded_send(c: _RiskCase) -> None:
    forged = Risk(id="RISK-999", title="forged", description="forged", severity="high", status="open", related_item_id=None, opened_at="2026-09-18")
    guarded_send(
        c.proposal_id, action_type="risk_log_write", target="RISK-999",
        send_fn=lambda: CsvRiskLog(c.world.risk_csv).create_risk(forged), db_path=c.db,
    )


def _approve_risk_by(who: str, severity: str | None) -> Callable[[_RiskCase], object]:
    def run(c: _RiskCase):
        return service.approve_and_send(c.proposal_id, approver_id=who, severity=severity, publisher=c.spy, policy=POLICY, db_path=c.db)

    return run


def _tracker_guarded_send(c: _RiskCase) -> None:
    forged = TrackerItem(id="PM-999", title="forged", status="blocked", sprint_id="sprint-13", created_at="2026-09-18")
    guarded_send(c.proposal_id, action_type="tracker_write", target="PM-999",
                 send_fn=lambda: TrackerMock(db_path=c.db).create_item(forged), db_path=c.db)


def _rewrite_tracker_payload(c: _RiskCase) -> None:
    payload = json.loads(c.world.row(c.proposal_id)[1])
    payload["items"] = [{**payload["items"][0], "body": "tampered after the decision"}, *payload["items"][1:]]
    ProposalStore(c.db).refresh_payload(c.proposal_id, payload=payload)


def _rewrite_risk_payload(c: _RiskCase) -> None:
    payload = json.loads(c.world.row(c.proposal_id)[1])
    payload["items"] = [{**payload["items"][0], "title": "tampered after the decision"}]
    ProposalStore(c.db).refresh_payload(c.proposal_id, payload=payload)


RISK_ATTEMPTS: list[Attempt] = [
    # -- against a PENDING risk-log proposal: nobody has approved it
    Attempt("guarded_send called directly", "pending", _risk_guarded_send),
    Attempt("send_approved (service)", "pending", _send_approved),
    Attempt("approve_and_send by someone who is not an approver", "pending", _approve_risk_by("mallory", None)),
    Attempt("approve_and_send with a severity by someone who is not an approver", "pending", _approve_risk_by("mallory", "high")),
    Attempt("approve_and_send with a blank approver", "pending", _approve_risk_by("  ", None)),
    Attempt("approve_and_send as the system's own approver id", "pending", _approve_risk_by("system:auto-approve", None)),
    Attempt("store.apply directly", "pending", _store("apply")),
    Attempt("auto_approve_and_send with auto-approve on (a risk entry is never automatic)", "pending", _auto(POLICY_AUTO)),
    Attempt("auto_approve_and_send with auto-approve off", "pending", _auto(POLICY)),
    Attempt("card handler with no authenticated user", "pending", _card({"severity": "high"}, None)),
    Attempt("card handler with a forged approver in the data", "pending", _card({"approver_id": APPROVER, "severity": "high"}, "mallory")),
    Attempt("service internals (_execute) called directly", "pending", _execute_internal),
    # -- against a REJECTED risk-log proposal: a person said no
    Attempt("guarded_send called directly", "rejected", _risk_guarded_send),
    Attempt("send_approved (service)", "rejected", _send_approved),
    Attempt("re-approved by an authorised approver", "rejected", _approve_risk_by(APPROVER, None)),
    Attempt("re-approved with a severity by an authorised approver", "rejected", _approve_risk_by(APPROVER, "high")),
    Attempt("store.approve directly", "rejected", _store("approve")),
    Attempt("store.apply directly", "rejected", _store("apply")),
    Attempt("auto_approve_and_send with auto-approve on", "rejected", _auto(POLICY_AUTO)),
    Attempt("card handler approve by an authorised user", "rejected", _card({"severity": "high"}, APPROVER)),
    Attempt("service internals (_execute) called directly", "rejected", _execute_internal),
    Attempt("payload rewritten after the decision", "rejected", _rewrite_risk_payload),
]


TRACKER_ATTEMPTS: list[Attempt] = [
    Attempt(a.name, a.state, _tracker_guarded_send if a.run is _risk_guarded_send else _rewrite_tracker_payload
            if a.run is _rewrite_risk_payload else a.run)
    for a in RISK_ATTEMPTS
]


def _try_batch(attempt: Attempt, propose: str) -> tuple[bool, str]:
    """(did a write land or did the attempt end unexpectedly, how it ended), on a fresh database, risk log and tracker."""
    with tempfile.TemporaryDirectory(prefix="pm_gc6_risk_") as tmp:
        world = _RiskWorld(Path(tmp))
        with _risk_log_at(world.risk_csv):
            proposal_id = getattr(world, propose)()
            if attempt.state == "rejected":
                service.reject(proposal_id, approver_id=APPROVER, reason="gc6", policy=POLICY, db_path=world.db)
            case = _RiskCase(world, proposal_id)
            before = world.state(proposal_id)
            refused, how = _how_it_ended(attempt, case)
            landed = _landed(
                before=before, after=world.state(proposal_id), sent_rows=world.sent_rows(proposal_id),
                adapter_calls=len(world.spy.calls), outbound_lines=world.spy.outbound_lines(),
            )
    return landed or not refused, how


def _measure_batch_bypasses(attempts: list[Attempt], propose: str, metric_id: str, what: str) -> MetricResult:
    totals = {"pending": [0, 0], "rejected": [0, 0]}
    how: Counter[str] = Counter()
    first: str | None = None
    for attempt in attempts:
        failed, label = _try_batch(attempt, propose)
        totals[attempt.state][0] += 1
        totals[attempt.state][1] += failed
        how[label] += 1
        if failed and first is None:
            first = f"{attempt.state}: {attempt.name} ({label})"
    bypassed = sum(failed for _, failed in totals.values())
    detail = "; ".join(f"{s}: {n - b} of {n} {what} write attempts blocked" for s, (n, b) in totals.items())
    detail += "; blocked by " + ", ".join(f"{label} x{count}" for label, count in sorted(how.items()))
    return MetricResult(
        metric_id=metric_id,
        name=f"direct {what} write attempts that got through on a pending or rejected proposal (hard zero)",
        measured=bypassed, target=0, comparator_name="at_most", passed=at_most(bypassed, 0),
        detail=detail + (f"; first bypass: {first}" if first else ""),
    )


def _measure_risk_bypasses() -> MetricResult:
    return _measure_batch_bypasses(RISK_ATTEMPTS, "propose_risk", RISK_BYPASS_ID, "risk-log")


def _measure_tracker_bypasses() -> MetricResult:
    return _measure_batch_bypasses(TRACKER_ATTEMPTS, "propose_tracker", TRACKER_BYPASS_ID, "tracker")


def _check_risk_decision(
    world: _RiskWorld, label: str, proposal_id: str, *, approver: str, window: tuple[datetime, datetime],
    original: list[dict], written: tuple[str, str] | None,
) -> list[str]:
    """Read the raw rows and the CSV for one decided risk-log proposal and report every field that is missing or wrong. `written` is
    (severity, where it came from) for an approval, None for a rejection."""
    gaps: list[str] = []
    status, payload_json, approver_id, decided_at = world.row(proposal_id)
    payload = json.loads(payload_json)
    decision_action = "proposal.rejected" if written is None else "proposal.approved"
    events = world.sql(
        "SELECT actor, action, details, created_at FROM audit WHERE entity_type = 'proposal' AND entity_id = ? ORDER BY id", proposal_id,
    )
    decision = next((e for e in events if e[1] == decision_action), None)
    new_rows = [r for r in world.csv_rows() if r["id"] not in world.seed_ids]
    runtime_new = [r for r in world.runtime_rows() if r[0] not in world.seed_ids]
    sent = world.sql("SELECT action_type, target FROM write_log WHERE proposal_id = ? AND status = 'sent' ORDER BY id", proposal_id)

    if approver_id != approver:
        gaps.append(f"{label}: approver is {approver_id!r}, expected {approver!r}")
    if decision is None or decision[0] != approver:
        gaps.append(f"{label}: approver not in the audit record ({decision_action})")
    if not _within(decided_at, window):
        gaps.append(f"{label}: timestamp {decided_at!r} missing or outside the decision window")
    if decision is None or not _within(decision[3], window):
        gaps.append(f"{label}: timestamp not in the audit record")
    if world.original_items(proposal_id) != original:
        gaps.append(f"{label}: original payload is not what the agent proposed")
    if world.spy.calls:
        gaps.append(f"{label}: a risk-log decision posted something to the adapter")

    if written is None:  # rejected: nothing applied anywhere
        if sent or status == "applied" or new_rows or runtime_new:
            gaps.append(f"{label}: something was applied to a rejected proposal")
        if payload.get("items") != original:
            gaps.append(f"{label}: applied payload changed on a rejected proposal")
    else:
        severity, source = written
        ids = [r["id"] for r in new_rows]
        if status != "applied":
            gaps.append(f"{label}: the proposal is {status!r}, not applied")
        if len(new_rows) != len(original):
            gaps.append(f"{label}: {len(new_rows)} entries written for {len(original)} proposed")
        for entry, item in zip(new_rows, original):
            wanted = (item["title"], item.get("related_item_id") or "", severity, "open")
            if (entry["title"], entry["related_item_id"], entry["severity"], entry["status"]) != wanted:
                gaps.append(f"{label}: {entry['id']} is not what was approved")
            if item["reference"]["message_id"] not in entry["description"]:
                gaps.append(f"{label}: {entry['id']} does not say which message justifies it")
        if [(r[0], r[7]) for r in runtime_new] != [(r["id"], r.get("owner") or None) for r in new_rows]:
            gaps.append(f"{label}: the runtime copy of the risk log does not hold what was written (the owner included)")
        if sent != [("risk_log_write", ",".join(ids))]:
            gaps.append(f"{label}: the write log does not record what was written ({sent})")
        details = json.loads(decision[2]) if decision else {}
        if (details.get("severity"), details.get("severity_source")) != (severity, source):
            gaps.append(f"{label}: the approval does not record the severity and where it came from")
        for entry, item in zip(new_rows, original):  # the owner: only what the proposal evidenced, nothing guessed
            if (entry.get("owner") or "") and not item.get("suggested_owner"):
                gaps.append(f"{label}: {entry['id']} was given an owner the proposal did not suggest")
            if not (entry.get("owner") or "") and item.get("suggested_owner"):
                gaps.append(f"{label}: {entry['id']} lost the owner the proposal suggested")
        applied = next((e for e in events if e[1] == "proposal.applied"), None)
        applied_details = json.loads(applied[2]) if applied else {}
        if applied is not None and not {"owners", "runtime_copy", "lead_store"} <= set(applied_details):
            gaps.append(f"{label}: the applied record does not say what happened to the owners, the runtime copy and the lead's table")
        if applied is None or applied[0] != approver or applied_details.get("risk_ids") != ids:
            gaps.append(f"{label}: the audit record does not say who applied which entries")
        if (applied_details.get("severity"), applied_details.get("severity_source")) != (severity, source):
            gaps.append(f"{label}: the applied record does not carry the severity")

    trail = audit_trail(proposal_id, db_path=world.db)  # the app's own view must agree with the raw rows
    if (trail.approver_id, trail.decided_at, trail.original_proposal.get("items")) != (approver_id, decided_at, original):
        gaps.append(f"{label}: the application's audit trail disagrees with the raw rows")
    return gaps


def _measure_risk_audit() -> MetricResult:
    gaps: list[str] = []
    with tempfile.TemporaryDirectory(prefix="pm_gc6_risk_audit_") as tmp:
        world = _RiskWorld(Path(tmp))
        with _risk_log_at(world.risk_csv):
            for label, severity, written in (
                ("approved with a severity", "high", ("high", "approver")),
                ("approved with no severity", None, ("medium", "default")),
                ("rejected", None, None),
            ):
                pid = world.propose_risk(named_item="PM-014" if label == "approved with a severity" else None)  # PM-014 has an assignee
                original = world.original_items(pid)
                before = datetime.now(timezone.utc)
                if written is None:
                    service.reject(pid, approver_id=APPROVER, reason="gc6", policy=POLICY, db_path=world.db)
                else:
                    service.approve_and_send(pid, approver_id=APPROVER, severity=severity, publisher=world.spy, policy=POLICY, db_path=world.db)
                gaps += _check_risk_decision(world, label, pid, approver=APPROVER, window=(before, datetime.now(timezone.utc)),
                                             original=original, written=written)
                world.seed_ids |= {r["id"] for r in world.csv_rows()}  # what this decision wrote is not "new" for the next one
    return MetricResult(
        metric_id=RISK_AUDIT_ID,
        name="audit-record fields missing or wrong across three decided risk-log proposals (hard zero)",
        measured=len(gaps), target=0, comparator_name="at_most", passed=at_most(len(gaps), 0),
        detail=(
            "checked approver, timestamp, original payload and what was written (the entries, the severity and where it came from, "
            "the owner, the write log, the runtime copy and the lead's table) for: approved with a severity, approved with no severity, rejected"
            + (f"; first gap: {gaps[0]}" if gaps else "")
        ),
    )


def _check_tracker_decision(
    world: _RiskWorld, label: str, proposal_id: str, *, approver: str, window: tuple[datetime, datetime], original: list[dict],
    before: tuple, expected_skips: int | None,
) -> list[str]:
    """Read the raw rows for one decided tracker batch and report every field that is missing or wrong. `expected_skips` is how many items
    the writer should have skipped as already there, or None for a rejection (nothing may have been written at all)."""
    gaps: list[str] = []
    status, payload_json, approver_id, decided_at = world.row(proposal_id)
    payload = json.loads(payload_json)
    decision_action = "proposal.rejected" if expected_skips is None else "proposal.approved"
    events = world.sql(
        "SELECT actor, action, details, created_at FROM audit WHERE entity_type = 'proposal' AND entity_id = ? ORDER BY id", proposal_id,
    )
    decision = next((e for e in events if e[1] == decision_action), None)
    items_before, comments_before = before
    new_items = [r for r in world.tracker_rows()[0] if r not in items_before]
    new_comments = [r for r in world.tracker_rows()[1] if r not in comments_before]
    sent = world.sql("SELECT action_type, target FROM write_log WHERE proposal_id = ? AND status = 'sent' ORDER BY id", proposal_id)

    if approver_id != approver:
        gaps.append(f"{label}: approver is {approver_id!r}, expected {approver!r}")
    if decision is None or decision[0] != approver:
        gaps.append(f"{label}: approver not in the audit record ({decision_action})")
    if not _within(decided_at, window):
        gaps.append(f"{label}: timestamp {decided_at!r} missing or outside the decision window")
    if decision is None or not _within(decision[3], window):
        gaps.append(f"{label}: timestamp not in the audit record")
    if world.original_items(proposal_id) != original:
        gaps.append(f"{label}: original payload is not what the agent proposed")
    if world.spy.calls:
        gaps.append(f"{label}: a tracker decision posted something to the adapter")

    if expected_skips is None:  # rejected: nothing applied anywhere
        if sent or status == "applied" or new_items or new_comments:
            gaps.append(f"{label}: something was applied to a rejected proposal")
        if payload.get("items") != original:
            gaps.append(f"{label}: applied payload changed on a rejected proposal")
    else:
        details = json.loads(next((e[2] for e in events if e[1] == "proposal.applied"), "{}"))
        applied_by = next((e[0] for e in events if e[1] == "proposal.applied"), None)
        if status != "applied":
            gaps.append(f"{label}: the proposal is {status!r}, not applied")
        creates = [i for i in original if i["kind"] == "create"]
        comments = [i for i in original if i["kind"] == "comment"]
        if len(new_items) != len(creates):
            gaps.append(f"{label}: {len(new_items)} items created for {len(creates)} proposed")
        for row, item in zip(new_items, creates):  # items: id, title, status, sprint_id, assignee_id, created_at, blocked_since, source_message_id
            wanted = (item["title"], item["status"], item["sprint_id"], None, item["reference"]["date"], item["reference"]["message_id"])
            if (row[1], row[2], row[3], row[4], row[5], row[7]) != wanted:
                gaps.append(f"{label}: {row[0]} is not what was approved (title, status, sprint, assignee, date and source message)")
            note = [c for c in new_comments if c[1] == row[0]]
            if len(note) != 1 or not note[0][3].startswith("Created by the PM agent from") or "ai-created" not in json.loads(note[0][4]):
                gaps.append(f"{label}: {row[0]} has no tagged note saying where it came from")
        written = [c for c in comments if not any(c["body"] == nc[3] and c["item_id"] == nc[1] for nc in new_comments)]
        if len(written) != expected_skips:
            gaps.append(f"{label}: {len(written)} comment(s) not written, expected {expected_skips} skipped as already there")
        for item in comments:
            match = [c for c in new_comments if c[1] == item["item_id"] and c[3] == item["body"]]
            if match and not {"from-channel", "ai-created"} <= set(json.loads(match[0][4])):
                gaps.append(f"{label}: the comment on {item['item_id']} is not tagged as written by the agent from the channel")
        if len(new_comments) != len(new_items) + len(comments) - expected_skips:
            gaps.append(f"{label}: {len(new_comments)} comments written, expected {len(new_items) + len(comments) - expected_skips}")
        touched = sorted({r[0] for r in new_items} | {c[1] for c in new_comments})
        if sent != [("tracker_write", ",".join(touched))]:
            gaps.append(f"{label}: the write log does not record what was written ({sent})")
        if applied_by != approver or details.get("created") != [r[0] for r in new_items] or len(details.get("skipped", [])) != expected_skips:
            gaps.append(f"{label}: the audit record does not say who applied what, and what was skipped")

    trail = audit_trail(proposal_id, db_path=world.db)  # the app's own view must agree with the raw rows
    if (trail.approver_id, trail.decided_at, trail.original_proposal.get("items")) != (approver_id, decided_at, original):
        gaps.append(f"{label}: the application's audit trail disagrees with the raw rows")
    return gaps


def _measure_tracker_audit() -> MetricResult:
    gaps: list[str] = []
    with tempfile.TemporaryDirectory(prefix="pm_gc6_tracker_audit_") as tmp:
        world = _RiskWorld(Path(tmp))
        with _risk_log_at(world.risk_csv):
            first_comment = None
            for label, expected_skips in (("approved", 0), ("approved with a comment already there", 1), ("rejected", None)):
                pid = world.propose_tracker(comment_text=first_comment if expected_skips == 1 else None)
                first_comment = first_comment or world.last_comment_text
                original = world.original_items(pid)
                before = world.tracker_rows()
                moment = datetime.now(timezone.utc)
                if expected_skips is None:
                    service.reject(pid, approver_id=APPROVER, reason="gc6", policy=POLICY, db_path=world.db)
                else:
                    service.approve_and_send(pid, approver_id=APPROVER, publisher=world.spy, policy=POLICY, db_path=world.db)
                gaps += _check_tracker_decision(world, label, pid, approver=APPROVER, window=(moment, datetime.now(timezone.utc)),
                                                original=original, before=before, expected_skips=expected_skips)
    return MetricResult(
        metric_id=TRACKER_AUDIT_ID,
        name="audit-record fields missing or wrong across three decided tracker batches (hard zero)",
        measured=len(gaps), target=0, comparator_name="at_most", passed=at_most(len(gaps), 0),
        detail=(
            "checked approver, timestamp, original payload and what was written (the items with their status, sprint, no assignee and source "
            "message; the tagged comments; what was skipped as already there; the write log) for: approved, approved with a comment already "
            "there, rejected"
            + (f"; first gap: {gaps[0]}" if gaps else "")
        ),
    )


def measure_gc6() -> list[MetricResult]:
    with tempfile.TemporaryDirectory(prefix="pm_gc6_") as tmp:
        world = _World(Path(tmp))
        messages = [_measure_bypasses(world), _measure_audit(world)]
    return [*messages, _measure_risk_bypasses(), _measure_risk_audit(), _measure_tracker_bypasses(), _measure_tracker_audit()]


def register(registry: GoldenCaseRegistry) -> None:
    registry.register(
        GoldenCase(
            case_id="GC6",
            description="Approval enforcement: writes (a post, or a risk-log entry) on pending/rejected proposals fail; the audit record is complete",
            measure_fn=measure_gc6,
        )
    )
