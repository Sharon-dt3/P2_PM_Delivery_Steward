"""Teams cards: the human surface for the approval gate.

Adaptive Card 1.5 documents (what Copilot Studio renders in Teams) and the
backend handler their Action.Submit data is posted to. The cards decide what a
person sees and can click; they decide nothing else. The handler takes WHO is
acting from the platform's authenticated identity (Copilot Studio's own user
context) and never from the submitted data, then calls the same service
functions as the command line -- enforcement stays in pm.approval.service.

Not built: the Copilot Studio agent itself (needs a tenant). Same status as
P1's CHN-25 connector; see docs/copilot_studio/ for the contract.
"""

from __future__ import annotations

from pathlib import Path

from pm.approval import service
from pm.approval.audit import AuditTrail
from pm.approval.service import ActionResult, ApprovalPolicy, PendingApproval
from pm.storage.db import DEFAULT_DB_PATH

_SCHEMA = "http://adaptivecards.io/schemas/adaptive-card.json"


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
            _text("Morning brief awaiting approval", size="Large", weight="Bolder"),
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


def pending_proposal_only_card(pending: PendingApproval) -> dict:
    """The card for a proposal that can be read and rejected but not carried out (a
    risk-log entry): no Approve, no edit box."""
    return _card(
        [
            _text(pending.summary, size="Large", weight="Bolder"),
            _text(pending.content, fontType="Monospace"),
            _text("Applying an approved risk entry is not built yet: it can only be rejected.", isSubtle=True),
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
    body = [
        _text(f"Morning brief {verb.lower()}", size="Large", weight="Bolder"),
        {
            "type": "FactSet",
            "facts": [
                {"title": verb + " by", "value": str(trail.approver_id)},
                {"title": "At", "value": str(trail.decided_at)},
                {"title": "Status", "value": trail.status},
                {"title": "Sent to", "value": trail.sent["target"] if trail.sent else "not sent"},
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
    "proposal_id": ..., "edited_content": ...}. `authenticated_user_id` is
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
        outcome: ActionResult = service.approve_and_send(
            proposal_id, approver_id=authenticated_user_id,
            edited_content=edited if isinstance(edited, str) else None,
            publisher=publisher, policy=policy, db_path=db_path,
        )
    else:
        reason = request.get("reason")
        outcome = service.reject(
            proposal_id, approver_id=authenticated_user_id,
            reason=reason if isinstance(reason, str) else None, policy=policy, db_path=db_path,
        )
    return _result(outcome.proposal_id, outcome.outcome, outcome.detail)


def handle_list_pending(request: dict, *, db_path: str | Path = DEFAULT_DB_PATH) -> dict:
    """request: {} -- every proposal awaiting a decision, each with its card."""
    return {
        "approvals": [
            {
                "proposal_id": p.proposal_id, "type": p.type, "target_channel": p.target_channel,
                "local_date": p.local_date, "created_at": p.created_at, "summary": p.summary,
                "card": pending_brief_card(p) if p.type in service.EXECUTABLE_TYPES else pending_proposal_only_card(p),
            }
            for p in service.list_pending_approvals(db_path=db_path)
        ]
    }
