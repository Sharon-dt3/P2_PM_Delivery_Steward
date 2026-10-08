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
from pm.reporting.end_of_day_facts import SECTION_ORDER as EOD_SECTION_ORDER
from pm.reporting.end_of_day_summary import EndOfDaySummary
from pm.reporting.morning_brief import SECTION_ORDER, MorningBrief
from pm.scheduling.config import ProjectScheduleConfig
from pm.storage.db import DEFAULT_DB_PATH

BRIEF_PROPOSAL_TYPE = "morning_brief_publish"
EOD_PROPOSAL_TYPE = "end_of_day_summary_publish"
WEEKLY_REPORT_PROPOSAL_TYPE = "weekly_status_report"  # a draft for a person to read: nothing sends it, so it can be reviewed and rejected, never approved
NUDGE_PROPOSAL_TYPE = "commitment_nudge"  # a reminder, sent as a direct message to the person who made the commitment
ESCALATION_PROPOSAL_TYPE = "commitment_escalation"  # an evidence bundle, sent as a direct message to the lead
DIRECT_MESSAGE_TYPES = frozenset({NUDGE_PROPOSAL_TYPE, ESCALATION_PROPOSAL_TYPE})

PROPOSED = "proposed"
ALREADY_PROPOSED = "already_proposed"
FAILED = "failed"
NOT_ATTEMPTED = "not_attempted"
AUTO_SENT = "auto_sent"  # approved by the system under auto-approve, and sent
AUTO_SEND_FAILED = "auto_send_failed"  # approved by the system; the send failed (retryable)


def format_brief_message(label: str, local_date: str, content: str) -> str:
    """The exact text posted for a morning brief: an optional label, a dated
    title, then the brief itself and nothing else."""
    return f"{label}Morning brief — {local_date}\n\n{content}"


def format_summary_message(label: str, local_date: str, content: str) -> str:
    """The exact text posted for an end-of-day summary: an optional label, a dated title, then the summary."""
    return f"{label}End-of-day summary — {local_date}\n\n{content}"


def propose_end_of_day_summary(
    summary: EndOfDaySummary,
    config: ProjectScheduleConfig,
    *,
    local_date: str,
    taken_at: str,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> tuple[Proposal, bool]:
    """Returns (proposal, created): one per target channel per local day, like the brief. Same shape as
    the brief's proposal (payload = what would be posted and where; original_model_output = the lines with
    their references and quotes and what grounding dropped), so the same gate handles both."""
    target = config.publish_channel_id or config.channel_id
    content = format_summary_message(config.message_label, local_date, summary.content)
    key = f"{EOD_PROPOSAL_TYPE}:{target}:{local_date}"
    store = ProposalStore(db_path)

    existing = store.get_by_idempotency_key(key)
    if existing is not None:
        return existing, False

    lines = [
        {"section": section, "reference_id": line.message_id, "text": line.text, "quote": line.quote}
        for section in EOD_SECTION_ORDER
        for line in summary.sections[section]
    ]
    proposal = store.create(
        type=EOD_PROPOSAL_TYPE,
        payload={
            "channel_id": config.channel_id,
            "target_channel": target,
            "local_date": local_date,
            "snapshot_taken_at": taken_at,
            "morning_taken_at": summary.facts.morning_taken_at,
            "content": content,
        },
        original_model_output={"content": content, "lines": lines, "dropped": summary.dropped, "changed_items": summary.facts.changed_ids},
        source_refs=sorted({line["reference_id"] for line in lines if line["reference_id"]}),
        idempotency_key=key,
    )
    write_audit(
        db_path, actor=AGENT, action="proposal.created", proposal_id=proposal.id,
        details={"type": EOD_PROPOSAL_TYPE, "target": target, "local_date": local_date},
    )
    return proposal, True


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


def propose_weekly_report(
    *, text: str, figures: list[dict], snapshots: dict[str, str], week_ending: str, narrative: dict | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> tuple[Proposal, bool]:
    """The weekly status report as a proposal: the text, the figures it states and the stored snapshots they were computed from. One per week
    ending. It is never sent: the type has no executor, so the gate offers it for review and rejection only."""
    key = f"{WEEKLY_REPORT_PROPOSAL_TYPE}:{week_ending}"
    store = ProposalStore(db_path)
    existing = store.get_by_idempotency_key(key)
    if existing is not None:
        return existing, False
    proposal = store.create(
        type=WEEKLY_REPORT_PROPOSAL_TYPE,
        payload={"local_date": week_ending, "content": text, "figures": figures, "snapshots": snapshots, "target_channel": "(not sent)"},
        original_model_output={"content": text, "figures": figures, "narrative": narrative},  # what the model wrote and what grounding dropped, untouched
        source_refs=sorted(snapshots.values()),
        idempotency_key=key,
    )
    write_audit(
        db_path, actor=AGENT, action="proposal.created", proposal_id=proposal.id,
        details={"type": WEEKLY_REPORT_PROPOSAL_TYPE, "week_ending": week_ending, "snapshots": snapshots},
    )
    return proposal, True
