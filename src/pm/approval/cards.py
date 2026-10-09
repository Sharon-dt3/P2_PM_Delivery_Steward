"""Teams cards: the human surface for the approval gate.

Adaptive Card 1.5 documents (what Copilot Studio renders in Teams) and the
backend handler their Action.Submit data is posted to. The cards decide what a
person sees and can click; they decide nothing else. The handler takes WHO is
acting from the platform's authenticated identity (Copilot Studio's own user
context) and never from the submitted data, then calls the same service
functions as the command line -- enforcement stays in pm.approval.service.

Three kinds of card: a message (a brief, a summary, a reminder: edit box, Approve, Reject), a risk-log
proposal (a severity picker, Approve, Reject: approving writes it to the risk log) and a tracker batch
(Approve, Reject: approving creates the items and adds the comments in the tracker). See docs/copilot_studio/ for the contract.
"""

from __future__ import annotations

from pathlib import Path

from pm.approval import service
from pm.approval.audit import AuditTrail
from pm.approval.proposals import (
    BRIEF_PROPOSAL_TYPE,
    EOD_PROPOSAL_TYPE,
    ESCALATION_PROPOSAL_TYPE,
    NUDGE_PROPOSAL_TYPE,
    WEEKLY_REPORT_PROPOSAL_TYPE,
)
from pm.approval.risk_apply import DEFAULT_SEVERITY
from pm.approval.service import ActionResult, ApprovalPolicy, PendingApproval
from pm.risklog.csv_store import SEVERITIES
from pm.storage.db import DEFAULT_DB_PATH

_SCHEMA = "http://adaptivecards.io/schemas/adaptive-card.json"


_CARD_TITLES = {
    BRIEF_PROPOSAL_TYPE: "Morning brief awaiting approval",
    EOD_PROPOSAL_TYPE: "End-of-day summary awaiting approval",
    NUDGE_PROPOSAL_TYPE: "Reminder awaiting approval",
    ESCALATION_PROPOSAL_TYPE: "Escalation to the lead awaiting approval",
}


def _card(body: list[dict], actions: list[dict] | None = None) -> dict:
    card = {"type": "AdaptiveCard", "$schema": _SCHEMA, "version": "1.5", "body": body}
    if actions:
        card["actions"] = actions
    return card


def _text(text: str, **extra) -> dict:
    return {"type": "TextBlock", "text": text, "wrap": True, **extra}


def pending_brief_card(pending: PendingApproval) -> dict:
    """The card shown for a brief awaiting a decision. The text box is
    pre-filled with the proposal: change it and press Approve to approve with
    edits; Reject needs no input."""
    return _card(
        [
            _text(_CARD_TITLES.get(pending.type, "Message awaiting approval"), size="Large", weight="Bolder"),
            {
                "type": "FactSet",
                "facts": [
                    {"title": "Posts to", "value": pending.target_channel},
                    {"title": "For", "value": pending.local_date},
                    {"title": "Proposed at", "value": pending.created_at},
                    {"title": "Proposal", "value": pending.proposal_id},
                ],
            },
            _text("What the agent proposed:", weight="Bolder"),
            _text(pending.content, fontType="Monospace"),
            {
                "type": "Input.Text",
                "id": "edited_content",
                "label": "Edit before approving (optional)",
                "isMultiline": True,
                "value": pending.content,
            },
        ],
        [
            {
                "type": "Action.Submit", "title": "Approve", "style": "positive",
                "data": {"action": "approve", "proposal_id": pending.proposal_id},
            },
            {
                "type": "Action.Submit", "title": "Reject", "style": "destructive", "associatedInputs": "none",
                "data": {"action": "reject", "proposal_id": pending.proposal_id},
            },
        ],
    )


def pending_risk_card(pending: PendingApproval) -> dict:
    """The card for a risk-log proposal: what would be written, a severity to pick (the agent proposes none; the default
    is medium and the audit records whether anyone chose), Approve (writes it to the risk log) and Reject. No edit box."""
    return _card(
        [
            _text(pending.summary, size="Large", weight="Bolder"),
            _text(pending.content, fontType="Monospace"),
            _text("Approving writes this to the risk log. The agent does not rate severity: you do.", isSubtle=True),
            {
                "type": "Input.ChoiceSet", "id": "severity", "label": "Severity", "style": "compact", "value": DEFAULT_SEVERITY,
                "choices": [{"title": s.capitalize(), "value": s} for s in SEVERITIES],
            },
        ],
        [
            {
                "type": "Action.Submit", "title": "Approve", "style": "positive",
                "data": {"action": "approve", "proposal_id": pending.proposal_id},
            },
            {
                "type": "Action.Submit", "title": "Reject", "style": "destructive", "associatedInputs": "none",
                "data": {"action": "reject", "proposal_id": pending.proposal_id},
            },
        ],
    )


def pending_tracker_card(pending: PendingApproval) -> dict:
    """The card for a batch of tracker changes: the whole list, each item with the channel message it came from and its full line (the
    title is clipped), then Approve (creates the items and adds the comments in the tracker) and Reject. No edit box, no severity."""
    return _card(
        [
            _text(pending.summary, size="Large", weight="Bolder"),
            _text(pending.content, fontType="Monospace"),
            _text("Approving writes these to the tracker: new items are blocked and have nobody assigned, and everything the agent "
                  "writes is tagged as created by the agent from that channel message. Items already there are skipped.", isSubtle=True),
        ],
        [
            {
                "type": "Action.Submit", "title": "Approve", "style": "positive", "associatedInputs": "none",
                "data": {"action": "approve", "proposal_id": pending.proposal_id},
            },
            {
                "type": "Action.Submit", "title": "Reject", "style": "destructive", "associatedInputs": "none",
                "data": {"action": "reject", "proposal_id": pending.proposal_id},
            },
        ],
    )


def _proposal_only_note(pending: PendingApproval) -> str:
    if pending.type == WEEKLY_REPORT_PROPOSAL_TYPE:
        return "This is a draft for you to read. The agent never sends it, so there is nothing to approve: you can only reject it."
    return "Applying an approved proposal of this kind is not built yet: it can only be rejected."


def pending_proposal_only_card(pending: PendingApproval) -> dict:
    """The card for a proposal that can be read and rejected but not carried out (a type nothing applies yet): no Approve, no edit box."""
    return _card(
        [
            _text(pending.summary, size="Large", weight="Bolder"),
            _text(pending.content, fontType="Monospace"),
            _text(_proposal_only_note(pending), isSubtle=True),
        ],
        [
            {
                "type": "Action.Submit", "title": "Reject", "style": "destructive", "associatedInputs": "none",
                "data": {"action": "reject", "proposal_id": pending.proposal_id},
            },
        ],
    )


def decision_card(trail: AuditTrail) -> dict:
    """The card shown once a decision is made: who, when, what was proposed,
    what was applied."""
    verb = {"rejected": "Rejected"}.get(trail.status, "Approved")
    writes_risk_log = trail.type in service.RISK_WRITE_TYPES
    writes_tracker = trail.type in service.TRACKER_WRITE_TYPES
    label = {EOD_PROPOSAL_TYPE: "End-of-day summary", NUDGE_PROPOSAL_TYPE: "Reminder", ESCALATION_PROPOSAL_TYPE: "Escalation"}.get(
        trail.type, "Tracker changes" if writes_tracker else "Risk-log proposal" if writes_risk_log else "Morning brief"
    )
    if writes_tracker:
        outcome = {"title": "Written to the tracker", "value": trail.sent["target"] if trail.sent else "not written"}
    elif writes_risk_log:
        outcome = {"title": "Written to the risk log as", "value": trail.sent["target"] if trail.sent else "not written"}
    else:
        outcome = {"title": "Sent to", "value": trail.sent["target"] if trail.sent else "not sent"}
    body = [
        _text(f"{label} {verb.lower()}", size="Large", weight="Bolder"),
        {
            "type": "FactSet",
            "facts": [
                {"title": verb + " by", "value": str(trail.approver_id)},
                {"title": "At", "value": str(trail.decided_at)},
                {"title": "Status", "value": trail.status},
                outcome,
            ],
        },
        _text("What the agent proposed:", weight="Bolder"),
        _text(trail.original_proposal.get("content", ""), fontType="Monospace"),
    ]
    if trail.edited:
        body += [_text("What was applied (edited):", weight="Bolder"), _text(trail.final_proposal.get("content", ""), fontType="Monospace")]
    else:
        body.append(_text("Applied as proposed.", isSubtle=True))
    return _card(body)


def _result(proposal_id: str, outcome: str, detail: str) -> dict:
    return {"proposal_id": proposal_id, "outcome": outcome, "detail": detail}


def handle_card_action(
    request: dict,
    *,
    authenticated_user_id: str | None,
    publisher=None,
    policy: ApprovalPolicy | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict:
    """What an Action.Submit posts back: {"action": "approve"|"reject",
    "proposal_id": ..., "edited_content": ..., "severity": ...} (severity only for a risk-log proposal). `authenticated_user_id` is
    supplied by the platform, never read from `request`; anything in the
    request that looks like an approver is ignored."""
    proposal_id = request.get("proposal_id") if isinstance(request, dict) else None
    action = request.get("action") if isinstance(request, dict) else None
    if not isinstance(proposal_id, str) or not proposal_id or action not in ("approve", "reject"):
        return _result(str(proposal_id or ""), service.REFUSED, "malformed request: need an action (approve or reject) and a proposal_id")
    if not isinstance(authenticated_user_id, str) or not authenticated_user_id.strip():
        return _result(proposal_id, service.REFUSED, "no authenticated user: refusing to act for an unknown person")

    if action == "approve":
        edited = request.get("edited_content")
        severity = request.get("severity")
        outcome: ActionResult = service.approve_and_send(
            proposal_id, approver_id=authenticated_user_id,
            edited_content=edited if isinstance(edited, str) else None,
            severity=severity if isinstance(severity, str) else None,
            publisher=publisher, policy=policy, db_path=db_path,
        )
    else:
        reason = request.get("reason")
        outcome = service.reject(
            proposal_id, approver_id=authenticated_user_id,
            reason=reason if isinstance(reason, str) else None, policy=policy, db_path=db_path,
        )
    return _result(outcome.proposal_id, outcome.outcome, outcome.detail)


def _card_for(pending: PendingApproval) -> dict:
    if pending.type in service.MESSAGE_TYPES:
        return pending_brief_card(pending)
    if pending.type in service.RISK_WRITE_TYPES:
        return pending_risk_card(pending)
    if pending.type in service.TRACKER_WRITE_TYPES:
        return pending_tracker_card(pending)
    return pending_proposal_only_card(pending)


def _listed(approvals) -> dict:
    return {
        "approvals": [
            {
                "proposal_id": p.proposal_id, "type": p.type, "target_channel": p.target_channel,
                "local_date": p.local_date, "created_at": p.created_at, "summary": p.summary,
                "card": _card_for(p),
            }
            for p in approvals
        ]
    }


def handle_list_pending(request: dict, *, db_path: str | Path = DEFAULT_DB_PATH) -> dict:
    """request: {} -- every proposal awaiting a decision, each with its card."""
    return _listed(service.list_pending_approvals(db_path=db_path))


def handle_claim_new(request: dict, *, db_path: str | Path = DEFAULT_DB_PATH) -> dict:
    """request: {} -- only the pending proposals no card has been sent for yet (or whose card is old enough to remind about), each with its
    card, and each recorded as handed out so the next call does not return it again (pm.approval.card_delivery)."""
    from pm.approval.card_delivery import claim_new_approvals

    return _listed(claim_new_approvals(db_path=db_path))
