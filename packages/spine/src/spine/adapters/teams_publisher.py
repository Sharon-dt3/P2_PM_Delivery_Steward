"""
Teams publish adapter -- interface (CHN-22)

Narrow interface for the only two things anything in this programme
ever needs to write into Teams: a channel post and a direct message.
Agent logic depends ONLY on this interface -- no Graph SDK type, no
Power Automate request/response shape, may leak past it. LogPublisher
and PowerAutomateTeamsPublisher are interchangeable behind it.

This is deliberately a separate interface from TeamsReader (CHN-03),
not a `post_message` method bolted onto it: "a read credential must
never be able to post" (see TeamsReader's own docstring) is a
permissions boundary that has to be visible in the code, not just in
whichever Graph application permission happens to be granted at
deploy time. Every capability that ever needs to write into Teams --
CHN-17's daily digest, CHN-21's nudges, CHN-23's escalations -- takes a
`publisher` of this type, never a `reader`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class TeamsPublisher(ABC):
    """Write-only access to Teams. A publish credential behind this
    interface is never asked to read anything -- see the separate read
    adapter (CHN-03) for that path."""

    @abstractmethod
    def post_channel_message(self, channel_id: str, content: str) -> dict:
        """Posts `content` into the channel identified by channel_id,
        as the flow bot (real implementation) or as one more line in an
        inspectable log (mock). Returns an implementation-defined dict
        describing the result; callers depend only on it being
        truthy/inspectable, never on specific keys beyond what they
        themselves put in, since guarded_send() is what decides whether
        this method is ever called at all, not what it returns."""

    @abstractmethod
    def post_direct_message(self, member_id: str, content: str) -> dict:
        """Sends `content` to member_id as a private message -- never a
        channel post. Same return-value contract as
        post_channel_message."""
