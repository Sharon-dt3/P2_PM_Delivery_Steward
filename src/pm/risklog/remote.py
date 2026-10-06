"""What the sync needs from the lead-facing store (Dataverse's role; Supabase here)."""

from __future__ import annotations

from typing import Protocol

from pm.adapters.risk_log import Risk


class RemoteUnavailableError(Exception):
    """The lead-facing store could not be reached or written. The message never
    contains the connection string."""


class RemoteRiskLog(Protocol):
    def list_risks(self) -> list[Risk]: ...

    def replace_all(self, risks: list[Risk]) -> None: ...
