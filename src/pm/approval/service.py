"""PM-13: the approval service -- the one place the gate is enforced.

Cards, the command line and any future surface call exactly these functions
and nothing else, so they cannot get different answers or leave different
records. Rules enforced here, never in a surface:

- only someone on the approver list may approve or reject (fail closed: with
  no approvers configured, nobody can); a refused attempt is itself recorded;
- who is acting is a parameter the caller's platform supplies; nothing a card
  submits can change it;
- a proposal is executed only after a positive approval, through spine's
  guarded_send (which refuses anything not approved, whoever calls it), and
  every attempt is logged (write_log) and audited;
- an edit replaces what is sent but never what the agent originally proposed;
- a real (non-log) publisher may only post to a channel on P1's allowlist, and
  that is checked BEFORE approving, so an out-of-scope proposal cannot be
  approved into a stuck state;
- approving a risk-log proposal writes to the risk log, and approving a tracker
  batch writes to the tracker, through the same gate (guarded_send), after
  pm.approval.risk_apply / tracker_apply has checked it can be written, so
  nothing is left approved-but-not-applied.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from p1.adapters.teams_publisher_mock import LogPublisher
from spine.approval.proposals import (
    APPLIED,
    APPROVED,
    PENDING,
    IllegalTransitionError,
    Proposal,
    ProposalNotFoundError,
    ProposalStore,
)
from spine.approval.write_guard import WriteRefusedError, guarded_send

from pm.adapters.risk_log import RiskLogStore
from pm.adapters.teams import get_teams_publisher
from pm.adapters.tracker import Tracker, TrackerMock
from pm.approval import risk_apply, tracker_apply
from pm.approval.audit import AGENT, AUTO_APPROVER, write_audit
from pm.approval.proposals import (
    BRIEF_PROPOSAL_TYPE,
    DIRECT_MESSAGE_TYPES,
    EOD_PROPOSAL_TYPE,
    ESCALATION_PROPOSAL_TYPE,
    NUDGE_PROPOSAL_TYPE,
    WEEKLY_REPORT_PROPOSAL_TYPE,
)
from pm.channel.batches import CHANNEL_RISK_PROPOSAL_TYPE, CHANNEL_TRACKER_PROPOSAL_TYPE
from pm.commitments import delivery
from pm.mirror.hook import mirrored
from pm.reporting.morning_brief import _AS_RECORDED
from pm.risk.proposals import RISK_PROPOSAL_TYPE
from pm.risklog.csv_store import CsvRiskLog
from pm.scheduling.config import P1_CHANNEL_CONFIG_DIR
from pm.storage.db import DEFAULT_DB_PATH

# What this service can carry out. A message is posted or sent (a brief, an end-of-day summary, a reminder, an escalation); a risk-log
# proposal is written to the risk log (pm.approval.risk_apply); a batch of tracker changes is written to the tracker
# (pm.approval.tracker_apply). Anything else has nothing to execute, so approving it is refused up front rather than left half-approved.
MESSAGE_TYPES = frozenset({BRIEF_PROPOSAL_TYPE, EOD_PROPOSAL_TYPE}) | DIRECT_MESSAGE_TYPES
RISK_WRITE_TYPES = risk_apply.RISK_WRITE_TYPES
TRACKER_WRITE_TYPES = tracker_apply.TRACKER_WRITE_TYPES
EXECUTABLE_TYPES = MESSAGE_TYPES | RISK_WRITE_TYPES | TRACKER_WRITE_TYPES
CHANNEL_BATCH_TYPES = frozenset({CHANNEL_TRACKER_PROPOSAL_TYPE, CHANNEL_RISK_PROPOSAL_TYPE})  # PM-26: batches from P1's outcome record

SENT = "sent"
APPLIED_OUTCOME = "applied"  # a risk-log or tracker proposal written to the risk log or the tracker
REJECTED_OUTCOME = "rejected"
REFUSED = "refused"
SEND_FAILED = "send_failed"
HELD = "held"  # auto-approve declined: a person has to decide



@dataclass(frozen=True)
class ApprovalPolicy:
    approver_ids: frozenset[str] = frozenset()
    allowlisted_channel_ids: list[str] = field(default_factory=list)
    auto_approve: bool = False  # PM_AUTO_APPROVE=1: the system approves safe briefs itself
    auto_approve_requires_first_human: bool = True  # a person must have approved one for the channel first


@dataclass(frozen=True)
class ActionResult:
    proposal_id: str
    outcome: str  # sent | rejected | refused | send_failed
    detail: str


@dataclass(frozen=True)
class PendingApproval:
    proposal_id: str
    type: str
    target_channel: str
    local_date: str
    created_at: str
    summary: str
    content: str


def load_approval_policy(config_dir: str | Path = P1_CHANNEL_CONFIG_DIR) -> ApprovalPolicy:
    """Approvers are the comma-separated ids in PM_APPROVER_IDS (none by
    default); the channel allowlist is P1's own (config/channels). Auto-approve
    is on only when PM_AUTO_APPROVE is exactly "1"; its first-approval rule is
    on unless PM_AUTO_APPROVE_REQUIRES_FIRST_HUMAN is exactly "0"."""
    from p1.config.loader import ChannelConfigStore

    raw = os.environ.get("PM_APPROVER_IDS", "")
    return ApprovalPolicy(
        approver_ids=frozenset(part.strip() for part in raw.split(",") if part.strip()),
        allowlisted_channel_ids=ChannelConfigStore(config_dir).list_allowlisted_channels(),
        auto_approve=os.environ.get("PM_AUTO_APPROVE", "") == "1",
        auto_approve_requires_first_human=os.environ.get("PM_AUTO_APPROVE_REQUIRES_FIRST_HUMAN", "1") != "0",
    )


def _is_approver(approver_id: str, policy: ApprovalPolicy) -> bool:
    who = (approver_id or "").strip()
    return bool(who) and who != AUTO_APPROVER and who in policy.approver_ids


def _not_executable(proposal: Proposal) -> str | None:
    if proposal.type in EXECUTABLE_TYPES:
        return None
    return (
        f"a {proposal.type} proposal cannot be approved: nothing carries it out, so approving it would do nothing. "
        "It can be reviewed and rejected"
    )


def _normalise(text: str) -> str:
    return text.replace("\r\n", "\n").strip()


def _batch_line(item: dict) -> str:
    """One item of a batch from P1's outcome record, as a person reads it: what it would do, and the message that justifies it."""
    ref = item["reference"]
    if item["kind"] == "comment":
        what = f"Comment on {item['item_id']}: {item['source_text']}"
    elif item["kind"] == "create":
        what = f"New item (blocked, nobody assigned): {item['title']}"
    else:
        owner = f" (suggested owner {item['suggested_owner']})" if item.get("suggested_owner") else ""
        what = f"New risk entry for {item.get('related_item_id') or 'no item'}{owner}: {item['title']}"
    line = f"- {what}\n    from {ref['channel_display_name']} message {ref['message_id']} on {ref['date']}"
    full = item.get("source_text")
    if item["kind"] != "comment" and full and full.strip() != item["title"].strip():
        line += f"\n    the whole line: {full}"  # the title is clipped; whoever approves reads what was actually said
    if item.get("changed_since"):
        line += f"\n    replaces an earlier wording ({item['changed_since']['earlier_status']}): {item['changed_since']['earlier_text']}"
    return line


def _summarize_batch(proposal: Proposal) -> PendingApproval:
    payload = proposal.payload
    items = payload.get("items", [])
    kind = "Tracker changes" if proposal.type == CHANNEL_TRACKER_PROPOSAL_TYPE else "Risk-log entries"
    return PendingApproval(
        proposal_id=proposal.id, type=proposal.type, target_channel=payload.get("channel_id", "?"), local_date=payload.get("date", "?"),
        created_at=proposal.created_at,
        summary=f"{kind} from {payload.get('channel_display_name', '?')} ({payload.get('date', '?')}): {len(items)} item{'' if len(items) == 1 else 's'}",
        content="\n".join(_batch_line(item) for item in items),
    )


def _summarize(proposal: Proposal) -> PendingApproval:
    payload = proposal.payload
    target = payload.get("target_channel", "?")
    date = payload.get("local_date", "?")
    named = payload.get("channel_display_name") or target  # a channel brief knows the channel's name; the id alone says nothing to a person
    summary = f"Morning brief for {date} to {named}"
    if proposal.type == EOD_PROPOSAL_TYPE:
        summary = f"End-of-day summary for {date} to {named}"
    if proposal.type == NUDGE_PROPOSAL_TYPE:
        summary = f"Reminder to {payload.get('recipient_name') or target} about commitment #{payload.get('commitment_id')}"
    if proposal.type == ESCALATION_PROPOSAL_TYPE:
        summary = f"Escalation to {payload.get('recipient_name') or target} about commitment #{payload.get('commitment_id')}"
    if proposal.type in CHANNEL_BATCH_TYPES:
        return _summarize_batch(proposal)
    if proposal.type == WEEKLY_REPORT_PROPOSAL_TYPE:
        summary = f"Weekly status report, week ending {date} (a draft: it is never sent)"
    if proposal.type == RISK_PROPOSAL_TYPE:
        summary = f"Proposed risk log entry for {payload.get('item_id', '?')} ({payload.get('blocker_ref', '?')})"
    return PendingApproval(
        proposal_id=proposal.id, type=proposal.type, target_channel=target, local_date=date,
        created_at=proposal.created_at, summary=summary, content=payload.get("content", ""),
    )


def list_pending_approvals(
    *, store: ProposalStore | None = None, db_path: str | Path = DEFAULT_DB_PATH
) -> list[PendingApproval]:
    store = store or ProposalStore(db_path)
    return [_summarize(p) for p in store.list_by_status(PENDING)]


def _deny(db_path, approver_id: str, proposal_id: str, attempted: str, why: str) -> ActionResult:
    write_audit(
        db_path, actor=(approver_id or "").strip() or "(blank)", action="proposal.denied",
        proposal_id=proposal_id, details={"attempted": attempted, "reason": why},
    )
    return ActionResult(proposal_id, REFUSED, why)


def _out_of_scope(proposal: Proposal, publisher, policy: ApprovalPolicy) -> str | None:
    """A real publisher may only post to an allowlisted channel; the log-only
    one posts nowhere, so it needs no allowlist."""
    if proposal.type in DIRECT_MESSAGE_TYPES:  # a message to one person, not a channel post: no channel allowlist, but a named recipient
        return None if proposal.payload.get("recipient_id") else "this message has no recipient"
    if isinstance(publisher, LogPublisher):
        return None
    target = proposal.payload.get("target_channel")
    if target not in policy.allowlisted_channel_ids:
        return f"{target} is not on the channel allowlist, so a real post to it is not allowed"
    return None


@mirrored
def approve_and_send(
    proposal_id: str,
    *,
    approver_id: str,
    edited_content: str | None = None,
    severity: str | None = None,
    publisher=None,
    policy: ApprovalPolicy | None = None,
    store: ProposalStore | None = None,
    risk_log: RiskLogStore | None = None,
    tracker: Tracker | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
    now: datetime | None = None,
) -> ActionResult:
    """Approve a pending proposal -- optionally with edited text -- and execute
    it in the same call: a message through the adapter, a risk-log proposal into the
    risk log (`severity` is the approver's rating for it; unrated means the default,
    and the audit says so), a tracker batch into the tracker."""
    policy = policy if policy is not None else load_approval_policy()
    store = store or ProposalStore(db_path)

    if not _is_approver(approver_id, policy):
        return _deny(db_path, approver_id, proposal_id, "approve", "not an authorised approver")
    approver = approver_id.strip()
    publisher_problem = None
    try:
        publisher = publisher if publisher is not None else get_teams_publisher()
    except Exception as exc:  # noqa: BLE001 - a misconfigured publisher means no message is approved, nothing raised
        publisher_problem = f"publisher not available: {type(exc).__name__}: {exc}"
    try:
        proposal = store.get(proposal_id)
    except ProposalNotFoundError:
        return ActionResult(proposal_id, REFUSED, publisher_problem or f"no proposal with id {proposal_id!r}")

    if proposal.type in RISK_WRITE_TYPES:  # writes to the risk log, posts nothing: a missing publisher does not matter
        return _approve_risk_entries(
            proposal, approver=approver, edited_content=edited_content, severity=severity, risk_log=risk_log, store=store, db_path=db_path,
        )
    if proposal.type in TRACKER_WRITE_TYPES:  # writes to the tracker, posts nothing; a severity means nothing here and is ignored
        return _approve_tracker_changes(proposal, approver=approver, edited_content=edited_content, tracker=tracker, store=store, db_path=db_path)
    if publisher_problem:
        return ActionResult(proposal_id, REFUSED, publisher_problem)

    unsupported = _not_executable(proposal)
    if unsupported:
        return _deny(db_path, approver_id, proposal_id, "approve", unsupported)
    scope_problem = _out_of_scope(proposal, publisher, policy)
    if scope_problem:
        return ActionResult(proposal_id, REFUSED, scope_problem)

    final_payload = None
    edited = False
    if edited_content is not None:
        text = _normalise(edited_content)
        if not text:
            return ActionResult(proposal_id, REFUSED, "the edited text is empty")
        if text != _normalise(proposal.payload.get("content", "")):
            edited = True
            final_payload = {**proposal.payload, "content": text}

    try:
        store.approve(proposal_id, approver_id=approver, payload=final_payload)
    except IllegalTransitionError as exc:
        return ActionResult(proposal_id, REFUSED, str(exc))

    if edited:
        write_audit(
            db_path, actor=approver, action="proposal.edited", proposal_id=proposal_id,
            details={
                "original_length": len(proposal.payload.get("content", "")),
                "final_length": len(final_payload["content"]),
            },
        )
    write_audit(db_path, actor=approver, action="proposal.approved", proposal_id=proposal_id, details={"edited": edited})
    return _execute(proposal_id, actor=approver, publisher=publisher, policy=policy, store=store, db_path=db_path, now=now)


def _approve_risk_entries(
    proposal: Proposal, *, approver: str, edited_content: str | None, severity: str | None, risk_log: RiskLogStore | None,
    store: ProposalStore, db_path,
) -> ActionResult:
    """Approve a risk-log proposal: check it can be written (and the severity is valid) BEFORE approving, record the approval with the
    severity and where it came from, then write. A refusal is audited and leaves the proposal pending."""
    pid = proposal.id
    # A risk card has no edit box, so a flow that maps the card's edited_content sends nothing or a blank: that is "not edited".
    if edited_content and _normalise(edited_content) and _normalise(edited_content) != _normalise(proposal.payload.get("content", "")):
        return _deny(db_path, approver, pid, "approve", "a risk-log proposal cannot be edited here: approve it as proposed, reject it, or change the risk log itself")
    try:
        chosen, source = risk_apply.choose_severity(severity)
        final_payload = {**proposal.payload, "approved_severity": chosen, "severity_source": source}
        risk_apply.plan_writes(
            replace(proposal, payload=final_payload), risk_log=risk_log or CsvRiskLog(), tracker=TrackerMock(db_path=db_path),
        )
    except risk_apply.RiskApplyRefused as exc:
        return _deny(db_path, approver, pid, "approve", str(exc))
    try:
        store.approve(pid, approver_id=approver, payload=final_payload)
    except IllegalTransitionError as exc:
        return ActionResult(pid, REFUSED, str(exc))
    write_audit(db_path, actor=approver, action="proposal.approved", proposal_id=pid,
                details={"edited": False, "severity": chosen, "severity_source": source})
    return _execute(pid, actor=approver, publisher=None, policy=None, store=store, db_path=db_path, risk_log=risk_log)


def _approve_tracker_changes(
    proposal: Proposal, *, approver: str, edited_content: str | None, tracker: Tracker | None, store: ProposalStore, db_path,
) -> ActionResult:
    """Approve a tracker batch: check it can be written BEFORE approving, record the approval, then write. A refusal is audited and leaves
    the proposal pending. A batch is approved whole or not at all: it cannot be edited here."""
    pid = proposal.id
    if edited_content and _normalise(edited_content) and _normalise(edited_content) != _normalise(proposal.payload.get("content", "")):
        return _deny(db_path, approver, pid, "approve", "a tracker batch cannot be edited here: approve it as proposed, or reject it")
    try:
        tracker_apply.plan_writes(proposal, tracker=tracker or TrackerMock(db_path=db_path))
    except tracker_apply.TrackerApplyRefused as exc:
        return _deny(db_path, approver, pid, "approve", str(exc))
    try:
        store.approve(pid, approver_id=approver)
    except IllegalTransitionError as exc:
        return ActionResult(pid, REFUSED, str(exc))
    write_audit(db_path, actor=approver, action="proposal.approved", proposal_id=pid, details={"edited": False})
    return _execute(pid, actor=approver, publisher=None, policy=None, store=store, db_path=db_path, tracker=tracker)


def approve_automatically(
    proposal_id: str, *, reason: str, store: ProposalStore | None = None, db_path: str | Path = DEFAULT_DB_PATH,
) -> None:
    """The system approves a pending reminder or escalation under the commitment follow-up's own rules (the person
    has been reminded before, within the cap). Recorded as automatic, under the system's own approver id, with the reason:
    never as a person. Raises IllegalTransitionError if it is not pending. Sending is a separate step (send_approved)."""
    store = store or ProposalStore(db_path)
    store.approve(proposal_id, approver_id=AUTO_APPROVER)
    write_audit(db_path, actor=AUTO_APPROVER, action="proposal.approved", proposal_id=proposal_id,
                details={"edited": False, "automatic": True, "reason": reason})


@mirrored
def reject(
    proposal_id: str,
    *,
    approver_id: str,
    reason: str | None = None,
    policy: ApprovalPolicy | None = None,
    store: ProposalStore | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> ActionResult:
    policy = policy if policy is not None else load_approval_policy()
    store = store or ProposalStore(db_path)
    if not _is_approver(approver_id, policy):
        return _deny(db_path, approver_id, proposal_id, "reject", "not an authorised approver")
    approver = approver_id.strip()
    try:
        store.reject(proposal_id, approver_id=approver)
    except ProposalNotFoundError:
        return ActionResult(proposal_id, REFUSED, f"no proposal with id {proposal_id!r}")
    except IllegalTransitionError as exc:
        return ActionResult(proposal_id, REFUSED, str(exc))
    write_audit(db_path, actor=approver, action="proposal.rejected", proposal_id=proposal_id, details={"reason": reason})
    return ActionResult(proposal_id, REJECTED_OUTCOME, "rejected")


@mirrored
def send_approved(
    proposal_id: str,
    *,
    publisher=None,
    policy: ApprovalPolicy | None = None,
    store: ProposalStore | None = None,
    risk_log: RiskLogStore | None = None,
    tracker: Tracker | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
    now: datetime | None = None,
) -> ActionResult:
    """Execute (or retry) an already-approved proposal. Refused for anything
    that is not approved -- pending, rejected, already sent, unknown. `now` is the
    clock a direct message's rules are judged by (a demo or a test can set it)."""
    policy = policy if policy is not None else load_approval_policy()
    store = store or ProposalStore(db_path)
    try:
        wanted = store.get(proposal_id)
    except ProposalNotFoundError:
        wanted = None
    if wanted is not None and wanted.type in RISK_WRITE_TYPES | TRACKER_WRITE_TYPES:  # a retry of a risk-log or tracker write posts nothing
        return _execute(proposal_id, actor=AGENT, publisher=None, policy=policy, store=store, db_path=db_path, risk_log=risk_log, tracker=tracker)
    try:
        publisher = publisher if publisher is not None else get_teams_publisher()
    except Exception as exc:  # noqa: BLE001 - reported, never raised past this seam
        return ActionResult(proposal_id, REFUSED, f"publisher not available: {type(exc).__name__}: {exc}")
    return _execute(proposal_id, actor=AGENT, publisher=publisher, policy=policy, store=store, db_path=db_path, now=now)


def _execute_direct_message(proposal: Proposal, *, actor: str, publisher, store: ProposalStore, db_path, now: datetime) -> ActionResult:
    """A reminder to the person, or an escalation to the lead: a direct message, sent only after the rules in
    pm.commitments.delivery (the shared cap, the commitment still open) and recorded there afterwards."""
    problem = delivery.pre_send_problem(proposal, db_path, now)
    if problem:
        write_audit(db_path, actor=actor, action="proposal.send_refused", proposal_id=proposal.id, details={"reason": problem})
        return ActionResult(proposal.id, REFUSED, problem)
    recipient, content = proposal.payload["recipient_id"], proposal.payload.get("content", "")
    action = "nudge" if proposal.type == NUDGE_PROPOSAL_TYPE else "escalation"
    try:
        guarded_send(
            proposal.id, action_type=action, target=recipient,
            send_fn=lambda: publisher.post_direct_message(recipient, content), store=store, db_path=db_path,
        )
    except WriteRefusedError as exc:
        return ActionResult(proposal.id, REFUSED, str(exc))
    except Exception as exc:  # noqa: BLE001 - guarded_send already logged send_failed; report, never raise
        write_audit(db_path, actor=actor, action="proposal.send_failed", proposal_id=proposal.id,
                    details={"error": f"{type(exc).__name__}: {exc}"[:300]})
        return ActionResult(proposal.id, SEND_FAILED, f"{type(exc).__name__}: {exc}")
    write_audit(db_path, actor=actor, action="proposal.sent", proposal_id=proposal.id, details={"target": recipient})
    delivery.after_send(proposal, db_path, now)
    return ActionResult(proposal.id, SENT, f"sent to {recipient}")


def _execute_risk_write(proposal: Proposal, *, actor: str, store: ProposalStore, db_path, risk_log: RiskLogStore | None) -> ActionResult:
    """Write an approved risk-log proposal to the risk log, through the same gate as a send: guarded_send refuses anything not approved,
    logs the attempt, and marks the proposal applied. What is written is planned again here from the approved proposal, so a retry
    after a failed write sees the log as it is now."""
    risk_log = risk_log or CsvRiskLog()
    try:
        plan = risk_apply.plan_writes(proposal, risk_log=risk_log, tracker=TrackerMock(db_path=db_path))
    except risk_apply.RiskApplyRefused as exc:
        write_audit(db_path, actor=actor, action="proposal.send_refused", proposal_id=proposal.id, details={"reason": str(exc)})
        return ActionResult(proposal.id, REFUSED, str(exc))
    ids = [r.id for r in plan.entries]

    def write() -> None:
        for entry in plan.entries:
            risk_log.create_risk(entry)

    try:
        guarded_send(proposal.id, action_type="risk_log_write", target=",".join(ids), send_fn=write, store=store, db_path=db_path)
    except WriteRefusedError as exc:
        return ActionResult(proposal.id, REFUSED, str(exc))
    except Exception as exc:  # noqa: BLE001 - guarded_send already logged send_failed; report, never raise
        write_audit(db_path, actor=actor, action="proposal.send_failed", proposal_id=proposal.id,
                    details={"error": f"{type(exc).__name__}: {exc}"[:300]})
        return ActionResult(proposal.id, SEND_FAILED, f"{type(exc).__name__}: {exc}")
    runtime = risk_apply.refresh_runtime_copy(risk_log, db_path)
    lead = risk_apply.sync_lead_store(db_path)  # the lead's table, when the sync is on; the CSV write above already stands either way
    write_audit(
        db_path, actor=actor, action="proposal.applied", proposal_id=proposal.id,
        details={"target": "risk_log", "risk_ids": ids, "severity": plan.severity, "severity_source": plan.severity_source,
                 "skipped": list(plan.skipped), "owners": list(plan.owners), "runtime_copy": runtime, "lead_store": lead},
    )
    skipped = f" ({len(plan.skipped)} already covered, not written)" if plan.skipped else ""
    if lead == "off" or lead == "skipped":
        where = ""
    elif lead in risk_apply.LEAD_LEVEL:
        where = "; the lead's table is up to date"
    else:
        where = f"; the lead's table is NOT up to date yet ({lead}): the next sync brings it level"
    return ActionResult(proposal.id, APPLIED_OUTCOME, f"wrote {', '.join(ids)} to the risk log{skipped}{where}")


def _execute_tracker_write(proposal: Proposal, *, actor: str, store: ProposalStore, db_path, tracker: Tracker | None) -> ActionResult:
    """Write an approved tracker batch to the tracker, through the same gate as a send: guarded_send refuses anything not approved, logs the
    attempt, and marks the proposal applied. What is written is planned again here from the approved proposal, so a retry after a failed
    write skips what already landed and finishes the rest."""
    tracker = tracker or TrackerMock(db_path=db_path)
    try:
        plan = tracker_apply.plan_writes(proposal, tracker=tracker)
    except tracker_apply.TrackerApplyRefused as exc:
        write_audit(db_path, actor=actor, action="proposal.send_refused", proposal_id=proposal.id, details={"reason": str(exc)})
        return ActionResult(proposal.id, REFUSED, str(exc))
    try:
        guarded_send(proposal.id, action_type="tracker_write", target=",".join(plan.touched), send_fn=lambda: tracker_apply.write(plan, tracker),
                     store=store, db_path=db_path)
    except WriteRefusedError as exc:
        return ActionResult(proposal.id, REFUSED, str(exc))
    except Exception as exc:  # noqa: BLE001 - guarded_send already logged send_failed; report, never raise
        write_audit(db_path, actor=actor, action="proposal.send_failed", proposal_id=proposal.id,
                    details={"error": f"{type(exc).__name__}: {exc}"[:300]})
        return ActionResult(proposal.id, SEND_FAILED, f"{type(exc).__name__}: {exc}")
    write_audit(
        db_path, actor=actor, action="proposal.applied", proposal_id=proposal.id,
        details={"target": "tracker", "created": plan.created, "commented": plan.commented, "skipped": list(plan.skipped),
                 **({"default_sprint": plan.default_sprint} if plan.default_sprint else {})},
    )
    parts = [f"created {', '.join(plan.created)}" if plan.created else "", f"added {len(plan.commented)} comment(s) on {', '.join(sorted(set(plan.commented)))}" if plan.commented else ""]
    skipped = f" ({len(plan.skipped)} skipped: already there or not writable)" if plan.skipped else ""
    placed = f"; new items went to {plan.default_sprint}, the configured default, because no sprint covers their day" if plan.default_sprint else ""
    return ActionResult(proposal.id, APPLIED_OUTCOME, "in the tracker: " + " and ".join(p for p in parts if p) + skipped + placed)


def _execute(
    proposal_id: str, *, actor: str, publisher, policy: ApprovalPolicy | None, store: ProposalStore, db_path, now: datetime | None = None,
    risk_log: RiskLogStore | None = None, tracker: Tracker | None = None,
) -> ActionResult:
    try:
        proposal = store.get(proposal_id)
    except ProposalNotFoundError:
        return ActionResult(proposal_id, REFUSED, f"no proposal with id {proposal_id!r}")
    if proposal.type in RISK_WRITE_TYPES:
        return _execute_risk_write(proposal, actor=actor, store=store, db_path=db_path, risk_log=risk_log)
    if proposal.type in TRACKER_WRITE_TYPES:
        return _execute_tracker_write(proposal, actor=actor, store=store, db_path=db_path, tracker=tracker)
    unsupported = _not_executable(proposal)  # defence in depth: whatever its status, only a message, a risk-log write or a tracker write is carried out
    if unsupported:
        return ActionResult(proposal_id, REFUSED, unsupported)
    scope_problem = _out_of_scope(proposal, publisher, policy)
    if scope_problem and proposal.status == APPROVED:
        return ActionResult(proposal_id, REFUSED, scope_problem)
    if proposal.type in DIRECT_MESSAGE_TYPES:
        return _execute_direct_message(proposal, actor=actor, publisher=publisher, store=store, db_path=db_path,
                                       now=now or datetime.now(timezone.utc))

    target = proposal.payload.get("target_channel", "")
    content = proposal.payload.get("content", "")
    try:
        guarded_send(
            proposal_id, action_type="channel_post", target=target,
            send_fn=lambda: publisher.post_channel_message(target, content), store=store, db_path=db_path,
        )
    except WriteRefusedError as exc:
        return ActionResult(proposal_id, REFUSED, str(exc))
    except Exception as exc:  # noqa: BLE001 - guarded_send already logged send_failed; report, never raise
        write_audit(
            db_path, actor=actor, action="proposal.send_failed", proposal_id=proposal_id,
            details={"error": f"{type(exc).__name__}: {exc}"[:300]},
        )
        return ActionResult(proposal_id, SEND_FAILED, f"{type(exc).__name__}: {exc}")

    write_audit(db_path, actor=actor, action="proposal.sent", proposal_id=proposal_id, details={"target": target})
    return ActionResult(proposal_id, SENT, f"sent to {target}")


def _held(proposal_id: str, why: str) -> ActionResult:
    return ActionResult(proposal_id, HELD, why)


def _dropped_count(proposal: Proposal) -> int:
    dropped = proposal.original_model_output.get("dropped") or {}
    return sum(len(failures) for failures in dropped.values())


def _a_person_has_approved_one_before(store: ProposalStore, proposal: Proposal) -> bool:
    target = proposal.payload.get("target_channel")
    return any(
        p.type in MESSAGE_TYPES
        and p.payload.get("target_channel") == target
        and p.approver_id not in (None, AUTO_APPROVER)
        for p in store.list_by_status(APPLIED)
    )


def _auto_hold_reason(proposal: Proposal, policy: ApprovalPolicy, publisher, store: ProposalStore) -> str | None:
    """Why auto-approve would NOT approve this pending proposal, or None if it
    would. The one place these rules live: auto_approve_and_send acts on it and
    explain_hold shows it, so they cannot disagree."""
    dropped = _dropped_count(proposal)
    if dropped or _AS_RECORDED in proposal.payload.get("content", ""):
        return f"grounding dropped {dropped} line(s) in this message; a person must review it"
    if policy.auto_approve_requires_first_human and not _a_person_has_approved_one_before(store, proposal):
        return "waiting for a person to approve a first brief for this channel"
    return _out_of_scope(proposal, publisher, policy)


def explain_hold(
    proposal_id: str,
    *,
    policy: ApprovalPolicy | None = None,
    publisher=None,
    store: ProposalStore | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> str | None:
    """Why this brief is waiting for a person, in words; None means nothing is
    holding it back (auto-approve would take it on its next run)."""
    policy = policy if policy is not None else load_approval_policy()
    store = store or ProposalStore(db_path)
    try:
        proposal = store.get(proposal_id)
    except ProposalNotFoundError:
        return f"no proposal with id {proposal_id!r}"
    if proposal.status != PENDING:
        return f"already {proposal.status}: nothing is waiting"
    if proposal.type in DIRECT_MESSAGE_TYPES:
        return "a reminder or an escalation: it is sent under the commitment follow-up's rules, or when a person approves it"
    if proposal.type not in MESSAGE_TYPES:
        return "this proposal changes the risk log or the tracker: auto-approve never takes it, so a person has to decide"
    if not policy.auto_approve:
        return "auto-approve is off, so a person has to decide"
    try:
        publisher = publisher if publisher is not None else get_teams_publisher()
    except Exception as exc:  # noqa: BLE001 - reported as the reason, not raised
        return f"publisher not available: {type(exc).__name__}: {exc}"
    return _auto_hold_reason(proposal, policy, publisher, store)


@mirrored
def auto_approve_and_send(
    proposal_id: str,
    *,
    publisher=None,
    policy: ApprovalPolicy | None = None,
    store: ProposalStore | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> ActionResult:
    """Unattended approval: the system approves a pending proposal itself and
    executes it -- only when that is safe, otherwise HELD for a person.

    Safe means: auto-approve is switched on; grounding dropped nothing and no
    fact fell back to "[as recorded]"; (unless switched off) a person has
    already approved a brief for this channel; and a real publisher's target is
    on the allowlist. The approval is recorded under AUTO_APPROVER, as
    automatic, with the reason -- never as a person -- and the text is never
    edited. Everything after that is the ordinary gate (guarded_send, logs)."""
    policy = policy if policy is not None else load_approval_policy()
    store = store or ProposalStore(db_path)
    if not policy.auto_approve:
        return _held(proposal_id, "auto-approve is off")
    try:
        publisher = publisher if publisher is not None else get_teams_publisher()
    except Exception as exc:  # noqa: BLE001 - a misconfigured publisher means it stays proposed
        return _held(proposal_id, f"publisher not available: {type(exc).__name__}: {exc}")
    try:
        proposal = store.get(proposal_id)
    except ProposalNotFoundError:
        return ActionResult(proposal_id, REFUSED, f"no proposal with id {proposal_id!r}")
    if proposal.status != PENDING:
        return ActionResult(proposal_id, REFUSED, f"proposal is {proposal.status!r}, not pending")
    if proposal.type in DIRECT_MESSAGE_TYPES:
        return _held(proposal_id, "a reminder or an escalation is approved by the commitment follow-up's own rules or by a person, never by brief auto-approve")
    if proposal.type not in MESSAGE_TYPES:
        return _held(proposal_id, "a proposal that changes the risk log or the tracker is never auto-approved; a person has to decide")

    held = _auto_hold_reason(proposal, policy, publisher, store)
    if held:
        return _held(proposal_id, held)

    reason = "every line grounded; " + (
        "a person had already approved a brief for this channel"
        if policy.auto_approve_requires_first_human else "the first-approval rule is switched off"
    )
    try:
        store.approve(proposal_id, approver_id=AUTO_APPROVER)
    except IllegalTransitionError as exc:
        return ActionResult(proposal_id, REFUSED, str(exc))
    write_audit(
        db_path, actor=AUTO_APPROVER, action="proposal.approved", proposal_id=proposal_id,
        details={"edited": False, "automatic": True, "reason": reason},
    )
    return _execute(proposal_id, actor=AUTO_APPROVER, publisher=publisher, policy=policy, store=store, db_path=db_path)
