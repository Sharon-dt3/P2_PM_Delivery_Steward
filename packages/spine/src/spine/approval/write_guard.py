"""
SPN-09: the service-layer write guard (Shared).

"Approval that is bypassable via the API is an automatic-failure
condition in the rubric, and here it also means messaging real
colleagues." This module is the one gate every outbound path -- a
channel post, a nudge, an escalation, and later P2/P3's own writes --
must call through before its side-effecting action ever runs. It knows
nothing about Teams, Power Automate, or what any particular action_type
actually does; guarded_send() takes the actual side effect as an opaque
send_fn and refuses to call it at all unless SPN-08's own record says
this proposal is currently approved.

"Regardless of caller" is the load-bearing phrase in this row: a caller
cannot opt out of the check, cannot pass a flag to skip it, and cannot
get a different answer by calling from CHN-17 vs. CHN-21 vs. CHN-23 --
there is exactly one function every one of those capabilities is meant
to route its outbound side effect through, and its logic never branches
on who is calling it.

Documented timeout behaviour: the lookup of a proposal's current status
is wrapped in a single broad except, deliberately, not narrowed to
ProposalNotFoundError alone. An unknown proposal id, a database error,
or (in a future networked ProposalStore) a lookup that times out are
all the same case from this function's point of view -- any failure to
positively confirm status == 'approved' defaults to refusing the send.
There is no code path anywhere in guarded_send() where an exception, an
ambiguous result, or elapsed time causes send_fn to run; approval must
be affirmatively confirmed, never merely "not yet refused." See
DECISION_LOG.md for why this is a deliberate fail-closed choice rather
than the usual advice to catch narrow exception types.

Every attempt -- refused, sent, or failed after being sent -- is
recorded in the write_log table (already present in the schema
alongside `proposals`, anticipating this row), so "every nudge,
escalation and digest is a row you can show on camera" -- CHN-22's own
rationale -- already holds true here, before CHN-22's actual adapter
exists.

On a successful send, guarded_send() also moves the proposal to
'applied' via SPN-08's own apply() -- so every future outbound path
that routes through this guard gets that bookkeeping for free, rather
than every one of CHN-17/CHN-21/CHN-23 needing to remember to call
apply() themselves after their own send succeeds.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from spine.approval.proposals import APPROVED, ProposalStore
from spine.storage.db import DEFAULT_DB_PATH, get_connection

logger = logging.getLogger("spine.approval.write_guard")

T = TypeVar("T")


class WriteRefusedError(Exception):
    """Raised whenever guarded_send() will not call send_fn -- a
    pending, rejected, or already-applied proposal; an unknown
    proposal_id; or any failure to positively confirm approval at all.
    Every one of these is a deliberate refusal, never a silent no-op
    and never a send that happens anyway."""


def guarded_send(
    proposal_id: str,
    *,
    action_type: str,
    target: str,
    send_fn: Callable[[], T],
    store: ProposalStore | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> T:
    """The one gate every outbound path must call through.

    action_type/target are for the write_log row only (e.g.
    action_type="channel_post"/target=channel_id, action_type="nudge"/
    target=member_id, action_type="escalation"/target=owner_id) --
    guarded_send() does not interpret them, it only records them.
    """
    store = store or ProposalStore(db_path)

    try:
        proposal = store.get(proposal_id)
    except Exception as exc:  # deliberately broad -- see module docstring
        _log_write(
            db_path,
            proposal_id=None,
            action_type=action_type,
            target=target,
            status="refused",
            details={"attempted_proposal_id": proposal_id, "reason": f"{type(exc).__name__}: {exc}"},
        )
        raise WriteRefusedError(
            f"could not confirm approval for proposal_id={proposal_id!r} ({type(exc).__name__}: {exc}); "
            "refusing to send -- no exception or lookup failure ever results in a send"
        ) from exc

    if proposal.status != APPROVED:
        _log_write(
            db_path,
            proposal_id=proposal_id,
            action_type=action_type,
            target=target,
            status="refused",
            details={"proposal_status": proposal.status},
        )
        raise WriteRefusedError(
            f"proposal_id={proposal_id!r} is status={proposal.status!r}, not 'approved'; "
            "refusing to send regardless of caller"
        )

    try:
        result = send_fn()
    except Exception as exc:
        _log_write(
            db_path,
            proposal_id=proposal_id,
            action_type=action_type,
            target=target,
            status="send_failed",
            # The reason is kept (2026-09-24): a send_failed row used to say
            # only "failed", so a repeat could not be diagnosed after the fact.
            details={"proposal_status": proposal.status, "error": f"{type(exc).__name__}: {exc}"[:500]},
        )
        raise

    store.apply(proposal_id)
    _log_write(
        db_path,
        proposal_id=proposal_id,
        action_type=action_type,
        target=target,
        status="sent",
        details={"payload": proposal.payload},
    )
    return result


def _log_write(
    db_path: str | Path,
    *,
    proposal_id: str | None,
    action_type: str,
    target: str,
    status: str,
    details: dict,
) -> None:
    conn = get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO write_log (proposal_id, action_type, target, payload, status) "
            "VALUES (?, ?, ?, ?, ?)",
            (proposal_id, action_type, target, json.dumps(details), status),
        )
        conn.commit()
    finally:
        conn.close()
    logger.info(
        "write_log proposal_id=%s action_type=%s target=%s status=%s", proposal_id, action_type, target, status,
    )
