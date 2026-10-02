"""
Teams read adapter -- interface (CHN-03)

Narrow interface for everything downstream needs from Teams: channels,
members, messages (with delta/since support), thread replies and
permalinks. Agent logic depends ONLY on this interface -- no Graph SDK
type or response shape may leak past it. MockTeamsReader and
GraphTeamsReader are interchangeable behind it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel


class DeltaTokenExpiredError(Exception):
    """Raised by a reader when a delta token it was given is no longer
    valid (e.g. Graph's HTTP 410 Gone). Part of the interface contract,
    not a Graph-only detail -- the caller's correct response is always
    the same regardless of which reader raised it: clear the persisted
    token for that channel and restart with a full sync (delta_token=None)."""


class DeltaLinkRejectedError(Exception):
    """Raised by a reader when a pagination continuation IT JUST HANDED
    BACK (an @odata.nextLink from the previous response, not a token the
    caller supplied from storage) is itself rejected on the very next
    call. Distinct from DeltaTokenExpiredError: that one is a
    previously-valid token going stale over time (HTTP 410), fixed by
    clearing it and doing a full resync. This one is the backend handing
    back a broken continuation link within the same sync attempt --
    resyncing from scratch does not help, because the same broken link
    shape gets produced again immediately (confirmed 2026-09-20 against
    a brand-new, empty Teams channel: Microsoft Graph's chatMessage
    delta endpoint returned an @odata.nextLink alongside an empty
    "value": [], then rejected that exact link with 400 "Parameter
    'DeltaToken' not supported for this request." -- a known, open
    Graph-side bug, not specific to this codebase; see
    https://learn.microsoft.com/en-us/answers/questions/1184831/ and
    DECISION_LOG.md's own 2026-09-20 entry).

    The correct response is to stop paging for this sync attempt --
    treat the last successfully-read page as the end -- and persist
    whatever delta position was already known-good before this
    rejection, not the rejected link itself."""


class TeamsChannel(BaseModel):
    id: str
    display_name: str


class TeamsMember(BaseModel):
    id: str
    display_name: str


class TeamsMessage(BaseModel):
    id: str
    channel_id: str
    author_id: str | None = None
    thread_root_id: str | None = None  # None if this is the root message
    posted_at: str  # ISO 8601, original post time -- never overwritten on edit
    edited_at: str | None = None
    deleted_at: str | None = None
    is_deleted: bool = False
    is_bot: bool = False
    is_system: bool = False
    body: str = ""
    permalink: str | None = None


class MessagePage(BaseModel):
    """One page of a list_messages() call: the messages, the token to pass
    back in next time, and whether more pages are available right now.

    has_more=True means there are more pages in THIS sync -- keep calling
    list_messages with the returned delta_token immediately. has_more=False
    means this is the final page -- persist the token and stop; it's for
    the *next* sync, not this one."""

    messages: list[TeamsMessage]
    delta_token: str
    has_more: bool = False


class TeamsReader(ABC):
    """Read-only access to allowlisted Teams channels. A read credential
    behind this interface must never be able to post -- see the separate
    publish adapter (CHN-22) for the write path."""

    @abstractmethod
    def list_channels(self) -> list[TeamsChannel]: ...

    @abstractmethod
    def list_channel_members(self, channel_id: str) -> list[TeamsMember]: ...

    @abstractmethod
    def list_messages(
        self,
        channel_id: str,
        since: str | None = None,
        delta_token: str | None = None,
    ) -> MessagePage:
        """since: ISO timestamp for an initial sync. delta_token: an opaque
        token from a prior call, for an incremental sync. If both are
        given, delta_token takes priority. May raise DeltaTokenExpiredError
        if delta_token is no longer valid."""

    @abstractmethod
    def list_replies(self, message_id: str) -> list[TeamsMessage]: ...

    @abstractmethod
    def get_permalink(self, message_id: str) -> str: ...
