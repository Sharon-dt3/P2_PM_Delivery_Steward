"""Names for the dashboard: people and channels by NAME, never by raw id.

An approver has to see who a nudge is about and where a message goes; a Teams
channel id or an Azure AD guid tells them nothing. Names come from P1's own
sources -- its channel config files and its member table. When a name cannot be
found the id itself is returned, so nothing is ever hidden. Looking is read-only:
a missing P1 database is not created.
"""

from __future__ import annotations

from pathlib import Path

from pm.adapters.teams import P1_REPO_ROOT

_CONFIG_DIR = P1_REPO_ROOT / "config" / "channels"


def channel_name(channel_id: str, p1_db: Path | None = None) -> str:
    try:
        from p1.config.loader import ChannelConfigStore

        store = ChannelConfigStore(_CONFIG_DIR)
        if p1_db is not None and Path(p1_db).exists():
            try:
                return store.get_effective_config(channel_id, db_path=p1_db).display_name
            except KeyError:
                pass  # not synced into that database: the channel's own config file still names it
        return store.get_channel_config(channel_id).display_name
    except Exception:  # noqa: BLE001 - an unknown channel is shown by its id
        return channel_id


def member_name(member_id: str, p1_db: Path | None = None) -> str:
    if p1_db is None or not Path(p1_db).exists():
        return member_id
    try:
        from p1.storage.members_repo import resolve_display_name

        return resolve_display_name(member_id, db_path=p1_db) or member_id
    except Exception:  # noqa: BLE001 - an unknown member is shown by id
        return member_id


def channel_owner_name(channel_id: str, p1_db: Path | None = None) -> str | None:
    """Who a channel's escalations go to, by name; None if it cannot be told."""
    try:
        from p1.config.loader import ChannelConfigStore

        store = ChannelConfigStore(_CONFIG_DIR)
        owner = None
        if p1_db is not None and Path(p1_db).exists():
            try:
                owner = store.get_effective_config(channel_id, db_path=p1_db).channel_owner_id
            except KeyError:
                owner = None
        owner = owner or store.get_channel_config(channel_id).channel_owner_id
    except Exception:  # noqa: BLE001 - an unknown channel has no known owner
        return None
    return member_name(owner, p1_db) if owner else None
