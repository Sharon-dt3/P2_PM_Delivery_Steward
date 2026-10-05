"""The morning brief as a proposal.

The scheduled job does not send the brief; it proposes it. One pending
proposal per target channel per local day (the idempotency key), carrying:

- payload: exactly the text that would be posted, and where;
- original_model_output: the same text plus each grounded line with its
  reference and quote, and what grounding dropped -- what the agent proposed,
  kept untouched whatever a person later edits;
- source_refs: every reference the brief's lines cite.
"""

from __future__ import annotations

from pathlib import Path

from spine.approval.proposals import Proposal, ProposalStore

from pm.approval.audit import AGENT, write_audit
from pm.reporting.morning_brief import SECTION_ORDER, MorningBrief
from pm.scheduling.config import ProjectScheduleConfig
from pm.storage.db import DEFAULT_DB_PATH

BRIEF_PROPOSAL_TYPE = "morning_brief_publish"

PROPOSED = "proposed"
ALREADY_PROPOSED = "already_proposed"
FAILED = "failed"
NOT_ATTEMPTED = "not_attempted"


def format_brief_message(label: str, local_date: str, content: str) -> str:
    """The exact text posted for a morning brief: an optional label, a dated
    title, then the brief itself and nothing else."""
    return f"{label}Morning brief — {local_date}\n\n{content}"


def propose_morning_brief(
    brief: MorningBrief,
    config: ProjectScheduleConfig,
    *,
    local_date: str,
    taken_at: str,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> tuple[Proposal, bool]:
    """Returns (proposal, created). `created` is False when a proposal for this
    target and day already exists (in any status): it is returned untouched."""
    target = config.publish_channel_id or config.channel_id
    content = format_brief_message(config.message_label, local_date, brief.content)
    key = f"{BRIEF_PROPOSAL_TYPE}:{target}:{local_date}"
    store = ProposalStore(db_path)

    existing = store.get_by_idempotency_key(key)
    if existing is not None:
        return existing, False

    lines = [
        {"section": section, "reference_id": line.message_id, "text": line.text, "quote": line.quote}
        for section in SECTION_ORDER
        for line in brief.sections[section]
    ]
    proposal = store.create(
        type=BRIEF_PROPOSAL_TYPE,
        payload={
            "channel_id": config.channel_id,
            "target_channel": target,
            "local_date": local_date,
            "snapshot_taken_at": taken_at,
            "content": content,
        },
        original_model_output={"content": content, "lines": lines, "dropped": brief.dropped},
        source_refs=sorted({line["reference_id"] for line in lines if line["reference_id"]}),
        idempotency_key=key,
    )
    write_audit(
        db_path, actor=AGENT, action="proposal.created", proposal_id=proposal.id,
        details={"type": BRIEF_PROPOSAL_TYPE, "target": target, "local_date": local_date},
    )
    return proposal, True
