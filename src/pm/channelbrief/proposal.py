"""A channel brief as a proposal: the same approval gate as every other message.

It is a morning brief or an end-of-day summary like any other (the same proposal types, the same card, the same approval, audit and post),
for a real channel, so everything downstream of here is code that already existed and is already tested. One proposal per channel per
local day per kind; the payload says it came from a channel record and which one, so it can always be traced back.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from spine.approval.proposals import Proposal, ProposalStore

from pm.approval.audit import AGENT, write_audit
from pm.approval.proposals import BRIEF_PROPOSAL_TYPE, EOD_PROPOSAL_TYPE
from pm.channelbrief.facts import MORNING, ChannelBriefFacts
from pm.channelbrief.render import evidence_lines, render_message
from pm.storage.db import DEFAULT_DB_PATH


def propose_channel_brief(
    facts: ChannelBriefFacts, *, target_channel: str | None = None, label: str = "", db_path: str | Path = DEFAULT_DB_PATH,
) -> tuple[Proposal, bool]:
    """Returns (proposal, created); `created` is False when this channel already has one for this day and kind (in any status)."""
    proposal_type = BRIEF_PROPOSAL_TYPE if facts.kind == MORNING else EOD_PROPOSAL_TYPE
    target = target_channel or facts.channel_id
    key = f"{proposal_type}:{target}:{facts.local_date}"
    store = ProposalStore(db_path)
    existing = store.get_by_idempotency_key(key)
    if existing is not None:
        return existing, False

    content = render_message(facts, label=label)
    lines = evidence_lines(facts)
    proposal = store.create(
        type=proposal_type,
        payload={
            "channel_id": facts.channel_id,
            "target_channel": target,
            "channel_display_name": facts.channel_name,
            "local_date": facts.local_date,
            "snapshot_taken_at": datetime.now(timezone.utc).isoformat(),
            "source": "channel_record",
            "record_date": facts.record_date,
            "content": content,
        },
        original_model_output={"content": content, "lines": lines, "dropped": {}},
        source_refs=sorted({line["reference_id"] for line in lines if line["reference_id"]}),
        idempotency_key=key,
    )
    write_audit(
        db_path, actor=AGENT, action="proposal.created", proposal_id=proposal.id,
        details={"type": proposal_type, "target": target, "local_date": facts.local_date, "source": "channel_record", "record_date": facts.record_date,
                 "sources": facts.sources},
    )
    return proposal, True
