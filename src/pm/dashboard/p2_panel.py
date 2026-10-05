"""The P2 tab: pending morning briefs, edit / approve / reject, retry, audit.

Thin: every button calls pm.approval.service -- the same functions the command
line and the Teams card use -- so the page can do nothing they cannot.
"""

from __future__ import annotations

import streamlit as st
from spine.approval.proposals import APPROVED, ProposalStore

from pm.approval import service
from pm.approval.audit import audit_trail, describe, recent_proposals


def _done(level: str, result: service.ActionResult) -> None:
    """Show the outcome once the page has refreshed with the new state."""
    st.session_state["flash"] = (level, f"{result.outcome}: {result.detail}")
    st.rerun()


def render(*, policy: service.ApprovalPolicy, acting_as: str | None, db_path: str) -> None:
    _pending(policy, acting_as, db_path)
    _unsent(policy, acting_as, db_path)
    st.divider()
    _audit(db_path)


def _pending(policy, acting_as, db_path) -> None:
    st.header("Awaiting a decision")
    pending = service.list_pending_approvals(db_path=db_path)
    if not pending:
        st.info("Nothing is awaiting approval.")
    for item in pending:
        with st.container(border=True):
            st.subheader(f"{item.local_date}  →  {item.target_channel}")
            created = item.created_at.split(".")[0].replace("T", " ") + " UTC"
            st.caption(f"proposal {item.proposal_id[:8]}… · proposed {created} · {item.type}")
            why = service.explain_hold(item.proposal_id, policy=policy, db_path=db_path)
            if why:
                st.caption(f"Waiting for you because: {why}.")
            else:
                st.caption("Nothing is holding this back: auto-approve will take it on its next run.")
            with st.expander("What the agent proposed (the exact text that will be posted)", expanded=True):
                st.text(item.content)
            edited = st.text_area(
                "Edit before approving (optional)", value=item.content, key=f"edit_{item.proposal_id}", height=200
            )
            is_edit = edited.replace("\r\n", "\n").strip() != item.content.replace("\r\n", "\n").strip()
            if is_edit:
                st.caption("You have edited the text: this version will be sent, and the original is kept in the audit trail.")
            reason = st.text_input("Reason, if rejecting (optional)", key=f"reason_{item.proposal_id}")
            approve_col, reject_col = st.columns(2)
            if acting_as:
                if approve_col.button("Approve", key=f"approve_{item.proposal_id}", type="primary"):
                    result = service.approve_and_send(
                        item.proposal_id, approver_id=acting_as, edited_content=edited if is_edit else None,
                        policy=policy, db_path=db_path,
                    )
                    _done("success" if result.outcome == service.SENT else "warning", result)
                if reject_col.button("Reject", key=f"reject_{item.proposal_id}"):
                    result = service.reject(
                        item.proposal_id, approver_id=acting_as, reason=reason or None, policy=policy, db_path=db_path
                    )
                    _done("warning", result)


def _unsent(policy, acting_as, db_path) -> None:
    unsent = ProposalStore(db_path).list_by_status(APPROVED)
    if not unsent:
        return
    st.header("Approved, not sent")
    st.caption("These were approved but the send failed. Retrying sends them once.")
    for proposal in unsent:
        with st.container(border=True):
            st.write(
                f"{proposal.payload.get('local_date', '?')} → {proposal.payload.get('target_channel', '?')} "
                f"· approved by {proposal.approver_id}"
            )
            if acting_as and st.button("Retry send", key=f"retry_{proposal.id}"):
                result = service.send_approved(proposal.id, policy=policy, db_path=db_path)
                _done("success" if result.outcome == service.SENT else "warning", result)


def _audit(db_path) -> None:
    st.header("Audit trail")
    recent = recent_proposals(limit=20, db_path=db_path)
    if not recent:
        st.info("No proposals yet.")
        return
    labels = {
        r.proposal_id: f"{r.local_date} · {r.status} · {r.approver_id or 'undecided'} · {r.proposal_id[:8]}"
        for r in recent
    }
    picked = st.selectbox("Proposal", [r.proposal_id for r in recent], format_func=labels.get, key="audit_pick")
    trail = audit_trail(picked, db_path=db_path)
    st.text(describe(trail))
    st.subheader("Everything recorded")
    for event in trail.events:
        extra = ", ".join(f"{k}={v}" for k, v in event["details"].items() if v not in (None, "", False))
        st.markdown(f"- `{event['created_at']}` · **{event['actor']}** · {event['action']}" + (f" ({extra})" if extra else ""))
    for attempt in trail.send_attempts:
        st.markdown(f"- send attempt `{attempt['created_at']}` · {attempt['status']} → {attempt['target']}")
