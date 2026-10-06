"""PM-16: one proposal per current blocker that has no risk-log entry.

Arithmetic in code, prose from the model. The gap set, every duration and the suggested
owner are computed by Python (pm.risk.gaps). The model only phrases the description and
the impact, and every line it writes must

- carry the blocker's own reference (reference-or-drop),
- carry a verbatim quote from that blocker's evidence,
- use only words, ids and numbers the evidence contains (the morning brief's check),
- state only durations Python computed and dates the evidence contains.

A line that fails is retried, then dropped; its half of the proposal is then filled by a
fixed template built from the same facts, so a real gap is never lost to a model failure.
The suggested owner is never the model's: it is the tracker's assignee, or nobody.

A proposal is only a proposal. Nothing is written to the risk log: approving it is
deliberately not wired to any action (see pm.approval.service.EXECUTABLE_TYPES).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel
from spine.approval.proposals import ProposalStore
from spine.grounding.kernel import FactualLine, ground_with_retry
from spine.llm.structured import generate_structured
from spine.prompts.registry import PromptRegistry

from pm.approval.audit import AGENT, write_audit
from pm.mirror.hook import mirrored
from pm.reporting.morning_brief import _check_line_content
from pm.risk.gaps import BlockerGap, find_gaps
from pm.state.snapshot import ProjectSnapshot
from pm.storage.db import DEFAULT_DB_PATH

logger = logging.getLogger(__name__)

RISK_PROPOSAL_TYPE = "risk_log_entry"
RISK_PROPOSAL_CAPABILITY = "pm16_risk_proposal"

_DURATION_RE = re.compile(r"(\d+)\s+days?\b", re.IGNORECASE)
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


class RiskLineDraft(BaseModel):
    kind: str  # description | impact
    text: str
    reference_id: str | None = None
    quote: str | None = None


class RiskDraft(BaseModel):
    lines: list[RiskLineDraft]


@dataclass(frozen=True)
class RiskProposalResult:
    item_id: str
    proposal_id: str
    created: bool
    prose_source: dict


def _check_risk_line(line: FactualLine, evidence: str, gap: BlockerGap) -> str | None:
    """The brief's content check, plus two the gap's arithmetic allows: any 'N days'
    must be a figure Python computed (a 9 hiding inside 2026-09-14 is not a duration),
    and any date must be one the evidence states."""
    problem = _check_line_content(line, evidence)
    if problem:
        return problem
    allowed = gap.allowed_durations()
    bad = [m for m in _DURATION_RE.findall(line.text) if int(m) not in allowed]
    if bad:
        return f"line states {bad[0]} days, which is not one of the computed durations ({', '.join(map(str, sorted(allowed))) or 'none'})"
    stray = [d for d in _ISO_DATE_RE.findall(line.text) if d not in evidence]
    if stray:
        return f"line states the date {stray[0]}, which is not in the evidence"
    return None


def _ask_model(gap: BlockerGap, gateway, prompt) -> tuple[dict[str, str], list[dict], list[dict]]:
    """Returns (grounded text by kind, every grounded line, what grounding dropped)."""
    evidence = gap.evidence_text()
    kinds: dict[str, str] = {}

    def generate_fn(feedback: str | None) -> list[FactualLine]:
        rendered = prompt.render(
            reference_id=gap.reference, evidence=evidence, feedback_block=f"\n{feedback}\n" if feedback else ""
        )
        draft = generate_structured(gateway, rendered, RiskDraft, tool_name="risk_log_entry")
        out = []
        for line in draft.lines:
            kinds[line.text] = line.kind
            out.append(FactualLine(text=line.text, message_id=line.reference_id, quote=line.quote))
        return out

    result = ground_with_retry(
        generate_fn, {gap.reference: evidence}.get, content_check=lambda line, source: _check_risk_line(line, source, gap)
    )
    chosen: dict[str, str] = {}
    lines = []
    for line in result.grounded_lines:
        kind = kinds.get(line.text)
        lines.append({"kind": kind, "text": line.text, "reference_id": line.message_id, "quote": line.quote})
        if kind in ("description", "impact") and kind not in chosen:
            chosen[kind] = line.text
    dropped = [{"reason": f.reason, "detail": f.detail, "text": f.line.text} for f in result.failures]
    return chosen, lines, dropped


def _readable(gap: BlockerGap, description: str, impact: str) -> str:
    owner = gap.owner
    owner_line = (
        f"{owner.name} ({owner.id}) - {owner.evidence}" if owner else "none - no owner is evidenced in the tracker"
    )
    return (
        f"Proposed risk log entry for {gap.item_id}\n"
        f"Blocker reference: {gap.reference}\n"
        f"Description: {description}\n"
        f"Impact: {impact}\n"
        f"Suggested owner: {owner_line}\n"
        f"Evidence: {gap.evidence_text()}"
    )


def _evidence_entries(gap: BlockerGap) -> list[dict]:
    entries = [{"ref": gap.reference, "text": gap.evidence_text()}]
    if gap.source_message:
        entries.append({"ref": f"message:{gap.source_message[0]}", "text": gap.message_sentence()})
    for sha, message in gap.commits:
        entries.append({"ref": f"commit:{sha}", "text": f'Commit {sha}: "{message}".'})
    for c, sentence in zip(gap.commitments, gap.commitment_sentences()):
        entries.append({"ref": f"commitment:{c.id}", "text": sentence})
    return entries


@mirrored
def detect_and_propose(
    snapshot: ProjectSnapshot,
    gateway,
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    prompt_registry: PromptRegistry | None = None,
) -> list[RiskProposalResult]:
    """One proposal per blocker missing from the risk log. Idempotent: a blocker already
    proposed (in any status, so also one a person rejected) is returned untouched and the
    model is not asked about it again. The same item blocked again from a new date is a
    new blockage and gets a new proposal."""
    gaps = find_gaps(snapshot)
    if not gaps:
        return []
    prompt = (prompt_registry or PromptRegistry()).get(RISK_PROPOSAL_CAPABILITY)
    store = ProposalStore(db_path)
    results = []
    for gap in gaps:
        key = f"{RISK_PROPOSAL_TYPE}:{gap.item_id}:{gap.blocked_since or 'unknown'}"
        existing = store.get_by_idempotency_key(key)
        if existing is not None:
            results.append(RiskProposalResult(gap.item_id, existing.id, False, existing.payload.get("prose_source", {})))
            continue

        try:
            chosen, lines, dropped = _ask_model(gap, gateway, prompt)
        except Exception as exc:  # noqa: BLE001 - a model that fails must not lose a real gap
            logger.warning("risk_prose_failed item=%s error=%s: %s", gap.item_id, type(exc).__name__, exc)
            chosen, lines, dropped = {}, [], [{"reason": "model_failed", "detail": f"{type(exc).__name__}: {exc}", "text": ""}]

        description = chosen.get("description") or gap.description_text()
        impact = chosen.get("impact") or gap.impact_text()
        prose_source = {
            "description": "model" if "description" in chosen else "template",
            "impact": "model" if "impact" in chosen else "template",
        }
        owner = gap.owner
        target = snapshot.channel.channel_id
        proposal = store.create(
            type=RISK_PROPOSAL_TYPE,
            payload={
                "blocker_ref": gap.reference,
                "item_id": gap.item_id,
                "description": description,
                "impact": impact,
                "suggested_owner": (
                    {"id": owner.id, "name": owner.name, "evidence": owner.evidence} if owner else None
                ),
                "evidence": _evidence_entries(gap),
                "facts": {
                    "as_of": gap.as_of, "blocked_since": gap.blocked_since, "days_blocked": gap.days_blocked,
                    "sprint_days_left": gap.sprint_days_left,
                },
                "prose_source": prose_source,
                "content": _readable(gap, description, impact),
                "local_date": gap.as_of,
                "channel_id": target,
                "target_channel": target,
                "snapshot_taken_at": snapshot.taken_at,
            },
            original_model_output={"description": description, "impact": impact, "lines": lines, "dropped": dropped},
            source_refs=sorted(gap.evidence_refs()),
            idempotency_key=key,
        )
        write_audit(
            db_path, actor=AGENT, action="proposal.created", proposal_id=proposal.id,
            details={"type": RISK_PROPOSAL_TYPE, "item": gap.item_id, "prose": prose_source},
        )
        results.append(RiskProposalResult(gap.item_id, proposal.id, True, prose_source))
    return results
