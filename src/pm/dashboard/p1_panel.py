"""The P1 tab: what P1 has waiting for a person, READ-ONLY.

It goes through P1's own approval service and P1's own database, and offers no
button: who may approve in P1 is decided in P1's repo, and nothing here changes
it. Looking never creates or writes anything: a missing database is reported, not
created, and only P1's list-pending function is called.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import streamlit as st

from pm.adapters.teams import P1_REPO_ROOT

P1_DASHBOARD_HINT = "cd ~/Desktop/P3_Agents && uv run streamlit run app/approval_dashboard.py"


@dataclass(frozen=True)
class P1View:
    db_path: Path
    pending: list | None  # None when P1's queue could not be read
    error: str | None


def p1_db_path() -> Path:
    """P1_DB_PATH, else P1's live database next to the P1 repo."""
    return Path(os.environ.get("P1_DB_PATH") or (P1_REPO_ROOT / "data" / "p1_live.db"))


def load_p1(db_path: Path | None = None) -> P1View:
    path = db_path or p1_db_path()
    if not path.exists():
        return P1View(path, None, f"P1 database not found at {path}")
    try:
        from p1.approval import service as p1_service

        return P1View(path, p1_service.list_pending_approvals(db_path=path), None)
    except Exception as exc:  # noqa: BLE001 - shown on the page, never a traceback
        return P1View(path, None, f"could not read P1's approvals ({type(exc).__name__}: {exc})")


def _display_name(channel_id: str, db_path: Path) -> str:
    try:
        from p1.config.loader import ChannelConfigStore

        return ChannelConfigStore(P1_REPO_ROOT / "config" / "channels").get_effective_config(
            channel_id, db_path=db_path
        ).display_name
    except Exception:  # noqa: BLE001 - an unknown channel is shown by its id
        return channel_id


def render(view: P1View) -> None:
    st.info(
        "This tab is read-only. To approve or reject a P1 item, use P1's own dashboard "
        f"(`app/approval_dashboard.py` in the P1 project): `{P1_DASHBOARD_HINT}`."
    )
    if view.error:
        st.warning(view.error)
        return
    if not view.pending:
        st.info("Nothing is waiting in P1.")
        return
    for item in view.pending:
        with st.container(border=True):
            channel = _display_name(item.channel_id, view.db_path)
            st.subheader(f"{item.type}  →  {channel}")
            created = item.created_at.split(".")[0].replace("T", " ") + " UTC"
            st.caption(f"proposal {item.proposal_id[:8]}… · proposed {created}")
            st.write(item.summary.replace(item.channel_id, channel))
            content = item.payload.get("content")
            if content:
                with st.expander("Message to be posted", expanded=False):
                    st.text(content)
