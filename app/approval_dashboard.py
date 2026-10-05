"""P2: the approval dashboard (Streamlit).

The human surface for the approval gate: pending morning briefs with the exact
text that would be posted, an edit box, Approve and Reject, a retry for an
approved brief whose send failed, and an audit view (who approved, when, what
the agent originally proposed versus what was applied).

Deliberately thin, like P1's fallback dashboard: every button calls straight into
pm.approval.service -- the same functions the command line (scripts/approve.py)
and the Teams card handler call -- with no rules of its own. Who may approve,
what a real publisher may post to, "an edit never overwrites the original" and
the audit records all live in the service, so this page can do nothing the other
surfaces cannot.

Who is acting: Streamlit has no sign-in of its own, so you choose from the
configured approvers (PM_APPROVER_IDS); the service still refuses anyone not on
that list. The audit trail is therefore only as trustworthy as that choice; for
production put the page behind company single sign-on.

Run:   uv run streamlit run app/approval_dashboard.py
Test seam: PM_DB_PATH points the page at another database (AppTest cannot pass
arguments to a script), as P1's P1_DB_PATH does.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (_REPO_ROOT / "src", _REPO_ROOT.parent / "P3_Agents" / "src", _REPO_ROOT / "packages" / "spine" / "src"):
    sys.path.insert(0, str(_path))

import streamlit as st
from dotenv import load_dotenv

# Streamlit is a long-lived process: without this the publisher and approver
# settings in .env would never be read (the mistake P1's dashboard once made).
load_dotenv()

from spine.approval.proposals import APPROVED, ProposalStore

from pm.approval import service
from pm.approval.audit import audit_trail, describe, recent_proposals
from pm.approval.settings import describe_settings
from pm.storage.db import DEFAULT_DB_PATH

DB_PATH = os.environ.get("PM_DB_PATH", str(DEFAULT_DB_PATH))

st.set_page_config(page_title="P2 approvals", layout="wide")
st.title("P2 delivery steward: approvals")

policy = service.load_approval_policy()
approvers = sorted(policy.approver_ids)

# --- the page's own banner: what is switched on, and who is acting ----------------------------

with st.sidebar:
    st.header("Settings in effect")
    for line in describe_settings():
        st.write(line)
    st.header("Who is acting")
    if approvers:
        default_user = os.environ.get("PM_DASHBOARD_USER", "")
        acting_as = st.selectbox(
            "Acting as", approvers, index=approvers.index(default_user) if default_user in approvers else 0,
            key="acting_as",
        )
    else:
        acting_as = None
        st.error("No approvers are configured (PM_APPROVER_IDS), so nobody can approve or reject from here.")

flash = st.session_state.pop("flash", None)
if flash:
    getattr(st, flash[0])(flash[1])


def _done(level: str, result: service.ActionResult) -> None:
    """Show the outcome after the page refreshes with the new state."""
    st.session_state["flash"] = (level, f"{result.outcome}: {result.detail}")
    st.rerun()


# --- pending approvals ----------------------------------------------------------------------

st.header("Awaiting a decision")
pending = service.list_pending_approvals(db_path=DB_PATH)
if not pending:
    st.info("Nothing is awaiting approval.")
for item in pending:
    with st.container(border=True):
        st.subheader(f"{item.local_date}  →  {item.target_channel}")
        created = item.created_at.split(".")[0].replace("T", " ") + " UTC"
        st.caption(f"proposal {item.proposal_id[:8]}… · proposed {created} · {item.type}")
        with st.expander("What the agent proposed (the exact text that will be posted)", expanded=True):
            st.text(item.content)
        edited = st.text_area("Edit before approving (optional)", value=item.content, key=f"edit_{item.proposal_id}", height=200)
        is_edit = edited.replace("\r\n", "\n").strip() != item.content.replace("\r\n", "\n").strip()
        if is_edit:
            st.caption("You have edited the text: this version will be sent, and the original is kept in the audit trail.")
        reason = st.text_input("Reason, if rejecting (optional)", key=f"reason_{item.proposal_id}")
        approve_col, reject_col = st.columns(2)
        if acting_as:
            if approve_col.button("Approve", key=f"approve_{item.proposal_id}", type="primary"):
                result = service.approve_and_send(
                    item.proposal_id, approver_id=acting_as, edited_content=edited if is_edit else None,
                    policy=policy, db_path=DB_PATH,
                )
                _done("success" if result.outcome == service.SENT else "warning", result)
            if reject_col.button("Reject", key=f"reject_{item.proposal_id}"):
                result = service.reject(
                    item.proposal_id, approver_id=acting_as, reason=reason or None, policy=policy, db_path=DB_PATH,
                )
                _done("warning", result)

# --- approved, but the send failed -----------------------------------------------------------------

unsent = ProposalStore(DB_PATH).list_by_status(APPROVED)
if unsent:
    st.header("Approved, not sent")
    st.caption("These were approved but the send failed. Retrying sends them once.")
    for proposal in unsent:
        with st.container(border=True):
            st.write(
                f"{proposal.payload.get('local_date', '?')} → {proposal.payload.get('target_channel', '?')} "
                f"· approved by {proposal.approver_id}"
            )
            if acting_as and st.button("Retry send", key=f"retry_{proposal.id}"):
                result = service.send_approved(proposal.id, policy=policy, db_path=DB_PATH)
                _done("success" if result.outcome == service.SENT else "warning", result)

# --- audit ---------------------------------------------------------------------------------------------

st.divider()
st.header("Audit trail")
recent = recent_proposals(limit=20, db_path=DB_PATH)
if not recent:
    st.info("No proposals yet.")
else:
    labels = {
        r.proposal_id: f"{r.local_date} · {r.status} · {r.approver_id or 'undecided'} · {r.proposal_id[:8]}" for r in recent
    }
    picked = st.selectbox("Proposal", [r.proposal_id for r in recent], format_func=labels.get, key="audit_pick")
    trail = audit_trail(picked, db_path=DB_PATH)
    st.text(describe(trail))
    st.subheader("Everything recorded")
    for event in trail.events:
        extra = ", ".join(f"{k}={v}" for k, v in event["details"].items() if v not in (None, "", False))
        st.markdown(f"- `{event['created_at']}` · **{event['actor']}** · {event['action']}" + (f" ({extra})" if extra else ""))
    for attempt in trail.send_attempts:
        st.markdown(f"- send attempt `{attempt['created_at']}` · {attempt['status']} → {attempt['target']}")
