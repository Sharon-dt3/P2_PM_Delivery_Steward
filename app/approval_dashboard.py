"""The approvals dashboard (Streamlit): one page, one sign-in, a tab per project.

  P2 morning brief   pending briefs with the exact text to be posted, an edit box,
                     Approve / Reject, retry for a failed send, and the audit trail
  P1 channel         what P1 has waiting -- READ-ONLY (approve in P1's own dashboard)

Deliberately thin, like P1's fallback dashboard: every P2 button calls straight into
pm.approval.service -- the same functions the command line (scripts/approve.py) and
the Teams card handler call -- with no rules of its own. Who may approve, what a real
publisher may post to, "an edit never overwrites the original" and the audit records
all live in the service. The P1 tab goes through P1's own service and database and
never writes to either.

Who is acting: Streamlit has no sign-in of its own, so you choose from the configured
approvers (PM_APPROVER_IDS); the service still refuses anyone not on that list. The
audit trail is therefore only as trustworthy as that choice; for production put the
page behind company single sign-on.

Run:   uv run streamlit run app/approval_dashboard.py
Test seams: PM_DB_PATH and P1_DB_PATH point the page at other databases (AppTest cannot
pass arguments to a script).
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

from pm.approval import service
from pm.approval.settings import describe_settings
from pm.dashboard import p1_panel, p2_panel
from pm.storage.db import DEFAULT_DB_PATH

DB_PATH = os.environ.get("PM_DB_PATH", str(DEFAULT_DB_PATH))

st.set_page_config(page_title="Approvals", layout="wide")
st.title("Approvals")

policy = service.load_approval_policy()
approvers = sorted(policy.approver_ids)

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

p1_view = p1_panel.load_p1()
p2_waiting = len(service.list_pending_approvals(db_path=DB_PATH))
p1_waiting = len(p1_view.pending) if p1_view.pending is not None else None

left, right = st.columns(2)
left.metric("P2: waiting", p2_waiting)
right.metric("P1: waiting", p1_waiting if p1_waiting is not None else "unavailable")

p2_tab, p1_tab = st.tabs(["P2 morning brief", "P1 channel"])
with p2_tab:
    p2_panel.render(policy=policy, acting_as=acting_as, db_path=DB_PATH)
with p1_tab:
    p1_panel.render(p1_view)
