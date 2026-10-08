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

Promotion (PM-19, pm.risk.promotion): given a PromotionPolicy, only blockers that are OLDER than
its configured threshold are proposed, the age is rebuilt from the status transitions, and each
proposal also carries a drafted mitigation and the evidence of how long the blocker has been open.
Without a policy this is exactly PM-16: every blocker missing from the risk log is proposed.

Rejection memory (PM-17, pm.risk.memory): a rejected proposal keeps the fingerprint of the
blocker's material facts, so a rerun does not propose it again; a material change allows
a new proposal that states, in code, what changed. A proposal still awaiting a decision
is not duplicated either.

A proposal is only a proposal until a person approves it. Nothing here writes to the risk log: approving one does, through the approval
gate (pm.approval.risk_apply), with the severity the approver chooses, since the proposal carries none.
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

from pm.approval.audit import AGENT, audit_trail, write_audit
from pm.mirror.hook import mirrored
from pm.reporting.morning_brief import _check_line_content
from pm.risk import memory
from pm.risk.gaps import BlockerGap, find_gaps
from pm.risk.promotion import PromotionCandidate, plan_promotion
from pm.risk.promotion_config import PromotionPolicy
from pm.state.snapshot import ProjectSnapshot
from pm.storage.db import DEFAULT_DB_PATH

logger = logging.getLogger(__name__)

RISK_PROPOSAL_TYPE = "risk_log_entry"
RISK_PROPOSAL_CAPABILITY = "pm16_risk_proposal"
PROMOTION_CAPABILITY = "pm19_risk_promotion"

# Plain management verbs a MITIGATION line may use besides the evidence's own words. They name
# an action, never a fact: no person, party, number, date or consequence is in this list.
MITIGATION_VOCABULARY = (
    "confirm agree assign resolve escalate prioritise prioritize review decide decision dependency plan unblock "
    "follow need needs needed ask request check update next step action set owner clear what how whether"
)

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
    state: str = memory.NEW  # new | already_proposed | rejected_unchanged | awaiting_decision | changed_since_rejection


def _check_risk_line(line: FactualLine, evidence: str, gap: BlockerGap, *, mitigation: bool = False) -> str | None:
    """The brief's content check, plus two the gap's arithmetic allows: any 'N days'
    must be a figure Python computed (a 9 hiding inside 2026-09-14 is not a duration),
    and any date must be one the evidence states. A mitigation line may also use the plain
    management verbs in MITIGATION_VOCABULARY, and nothing else the evidence does not say."""
    problem = _check_line_content(line, f"{evidence} {MITIGATION_VOCABULARY}" if mitigation else evidence)
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


def _ask_model(gap: BlockerGap, gateway, prompt, *, kinds_wanted=("description", "impact")) -> tuple[dict[str, str], list[dict], list[dict]]:
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
        generate_fn, {gap.reference: evidence}.get,
        content_check=lambda line, source: _check_risk_line(line, source, gap, mitigation=kinds.get(line.text) == "mitigation"),
    )
    chosen: dict[str, str] = {}
    lines = []
    for line in result.grounded_lines:
        kind = kinds.get(line.text)
        lines.append({"kind": kind, "text": line.text, "reference_id": line.message_id, "quote": line.quote})
        if kind in kinds_wanted and kind not in chosen:
            chosen[kind] = line.text
    dropped = [{"reason": f.reason, "detail": f.detail, "text": f.line.text} for f in result.failures]
    return chosen, lines, dropped


def _readable(gap: BlockerGap, description: str, impact: str, change: dict | None = None,
              promotion: dict | None = None) -> str:
    owner = gap.owner
    owner_line = (
        f"{owner.name} ({owner.id}) - {owner.evidence}" if owner else "none - no owner is evidenced in the tracker"
    )
    return (
        f"Proposed risk log entry for {gap.item_id}\n"
        f"Blocker reference: {gap.reference}\n"
        f"Description: {description}\n"
        f"Impact: {impact}\n"
        + (
            f"Open for: {_days(promotion['age']['days'])} (older than the {promotion['age']['threshold_days']}-day threshold; "
            f"blocked since {promotion['age']['entered_on']})\n"
            f"Drafted mitigation: {promotion['mitigation']}\n"
            if promotion else ""
        )
        + f"Suggested owner: {owner_line}\n"
        + (f"{change['text']}\n" if change else "")
        + f"Evidence: {gap.evidence_text()}"
    )


def _days(n: int) -> str:
    return f"{n} day" if n == 1 else f"{n} days"


def _age_evidence(candidate: PromotionCandidate, policy: PromotionPolicy, as_of: str) -> dict:
    age, tracker = candidate.age, candidate.tracker_blocked_since
    return {
        "days": age.days, "threshold_days": policy.threshold_days, "entered_on": age.entered_on, "entered_at": age.entered_at,
        "from_status": age.from_status, "source": age.source, "as_of": as_of,
        "tracker_blocked_since": tracker, "disagrees": (tracker or "")[:10] != age.entered_on,
    }


def _evidence_entries(gap: BlockerGap) -> list[dict]:
    entries = [{"ref": gap.reference, "text": gap.evidence_text()}]
    if gap.transition_note:
        entries.append({"ref": f"transition:{gap.item_id}", "text": gap.transition_note})
    if gap.source_message:
        entries.append({"ref": f"message:{gap.source_message[0]}", "text": gap.message_sentence()})
    for sha, message in gap.commits:
        entries.append({"ref": f"commit:{sha}", "text": f'Commit {sha}: "{message}".'})
    for c, sentence in zip(gap.commitments, gap.commitment_sentences()):
        entries.append({"ref": f"commitment:{c.id}", "text": sentence})
    return entries


def _earlier_proposals(store: ProposalStore, item_id: str) -> list:
    return [
        p for status in ("pending", "approved", "rejected", "applied") for p in store.list_by_status(status)
        if p.type == RISK_PROPOSAL_TYPE and p.payload.get("item_id") == item_id
    ]


def _rejection_reason(proposal_id: str, db_path) -> str | None:
    try:
        events = audit_trail(proposal_id, db_path=db_path).events
    except Exception:  # noqa: BLE001 - the reason is a courtesy; the change statement stands without it
        return None
    return next((e["details"].get("reason") for e in reversed(events) if e["action"] == "proposal.rejected"), None)


def recall_for(gap: BlockerGap, *, db_path: str | Path = DEFAULT_DB_PATH) -> memory.Memory:
    """What the store remembers about this blocker: see pm.risk.memory."""
    return memory.recall(gap, _earlier_proposals(ProposalStore(db_path), gap.item_id))


@mirrored
def detect_and_propose(
    snapshot: ProjectSnapshot,
    gateway,
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    prompt_registry: PromptRegistry | None = None,
    promotion: PromotionPolicy | None = None,
) -> list[RiskProposalResult]:
    """One proposal per blocker missing from the risk log. Idempotent, and it remembers
    rejections (PM-17): a blocker already proposed with these same material facts, in any
    status, is returned untouched and the model is not asked about it again; one whose
    earlier proposal is still awaiting a decision is not piled on; one whose every earlier
    proposal was rejected is proposed again only if its material facts changed, and the
    new proposal states the change."""
    candidates: dict[str, PromotionCandidate] = {}
    if promotion is None:
        gaps = find_gaps(snapshot)
    else:
        plan = plan_promotion(snapshot, promotion, db_path=db_path)
        snapshot = plan.snapshot  # blocked_since rebuilt from the transitions
        candidates = {c.gap.item_id: c for c in plan.eligible}
        gaps = [c.gap for c in plan.eligible]
    if not gaps:
        return []
    prompt = (prompt_registry or PromptRegistry()).get(RISK_PROPOSAL_CAPABILITY if promotion is None else PROMOTION_CAPABILITY)
    kinds_wanted = ("description", "impact") if promotion is None else ("description", "impact", "mitigation")
    store = ProposalStore(db_path)
    results = []
    for gap in gaps:
        recalled = memory.recall(gap, _earlier_proposals(store, gap.item_id))
        if recalled.state not in (memory.NEW, memory.CHANGED_SINCE_REJECTION):
            held = recalled.proposal
            results.append(RiskProposalResult(gap.item_id, held.id, False, held.payload.get("prose_source", {}), recalled.state))
            continue
        change = None
        if recalled.state == memory.CHANGED_SINCE_REJECTION:
            change = memory.change_statement(recalled, rejection_reason=_rejection_reason(recalled.proposal.id, db_path))
        key = f"{RISK_PROPOSAL_TYPE}:{gap.item_id}:{memory.fingerprint(gap)}"

        try:
            chosen, lines, dropped = _ask_model(gap, gateway, prompt, kinds_wanted=kinds_wanted)
        except Exception as exc:  # noqa: BLE001 - a model that fails must not lose a real gap
            logger.warning("risk_prose_failed item=%s error=%s: %s", gap.item_id, type(exc).__name__, exc)
            chosen, lines, dropped = {}, [], [{"reason": "model_failed", "detail": f"{type(exc).__name__}: {exc}", "text": ""}]

        description = chosen.get("description") or gap.description_text()
        impact = chosen.get("impact") or gap.impact_text()
        prose_source = {
            "description": "model" if "description" in chosen else "template",
            "impact": "model" if "impact" in chosen else "template",
        }
        promoted = None
        if promotion is not None:
            mitigation = chosen.get("mitigation") or gap.mitigation_text()
            prose_source["mitigation"] = "model" if "mitigation" in chosen else "template"
            promoted = {"age": _age_evidence(candidates[gap.item_id], promotion, gap.as_of), "mitigation": mitigation}
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
                "fingerprint": memory.fingerprint(gap),
                "material_facts": memory.material_facts(gap),
                "change": change,
                **({"mitigation": promoted["mitigation"], "age": promoted["age"],
                    "promotion": {"threshold_days": promotion.threshold_days, "source": promotion.source}} if promoted else {}),
                "content": _readable(gap, description, impact, change, promoted),
                "local_date": gap.as_of,
                "channel_id": target,
                "target_channel": target,
                "snapshot_taken_at": snapshot.taken_at,
            },
            original_model_output={
                "description": description, "impact": impact, **({"mitigation": promoted["mitigation"]} if promoted else {}),
                "lines": lines, "dropped": dropped,
            },
            source_refs=sorted([*gap.evidence_refs(), *([f"transition:{gap.item_id}"] if gap.transition_note else []),
                                *([f"proposal:{recalled.proposal.id}"] if change else [])]),
            idempotency_key=key,
        )
        write_audit(
            db_path, actor=AGENT, action="proposal.created", proposal_id=proposal.id,
            details={
                "type": RISK_PROPOSAL_TYPE, "item": gap.item_id, "prose": prose_source,
                "fingerprint": memory.fingerprint(gap),
                **({"follows_rejected": recalled.proposal.id} if change else {}),
            },
        )
        results.append(RiskProposalResult(gap.item_id, proposal.id, True, prose_source, recalled.state))
    return results
