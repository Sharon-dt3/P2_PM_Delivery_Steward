"""PM-19: which blockers are old enough to be promoted to a risk.

A current blocker that is not in the risk log is promoted once it has been blocked for MORE
days than the configured threshold (pm.risk.promotion_config). Python decides, from two things
only: the age, rebuilt from the item's status transitions (pm.risk.age), and the threshold, read
from configuration. No model is involved and no day count is written into this module.

The plan also says who is NOT promoted and why (not yet old enough, already in the risk log, age
cannot be established), so a person can see the decision, not just its result.

The snapshot the proposals are made from has each blocker's `blocked_since` replaced by the date
the transitions give, so the age, the evidence text and the PM-17 fingerprint all agree with
each other instead of with the tracker's stored field.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from pm.adapters.tracker import TrackerMock
from pm.risk.age import FROM_CREATION, BlockedAge, compute_blocked_age
from pm.risk.gaps import BlockerGap, current_blockers, find_gaps
from pm.risk.promotion_config import PromotionPolicy
from pm.state.moments import parse_moment
from pm.state.snapshot import ProjectSnapshot


@dataclass(frozen=True)
class PromotionCandidate:
    gap: BlockerGap
    age: BlockedAge
    tracker_blocked_since: str | None  # what the tracker's own field says, kept only to show a disagreement


@dataclass(frozen=True)
class PromotionPlan:
    policy: PromotionPolicy
    snapshot: ProjectSnapshot  # blocked_since rebuilt from the transitions
    eligible: list[PromotionCandidate] = field(default_factory=list)  # older than the threshold: to be proposed
    below_threshold: list[PromotionCandidate] = field(default_factory=list)  # not yet older than the threshold
    age_unknown: list[tuple[BlockerGap, BlockedAge]] = field(default_factory=list)  # history cannot give an age
    already_logged: list[str] = field(default_factory=list)  # blockers that already have a risk-log entry


def _transition_note(age: BlockedAge) -> str:
    if age.source == FROM_CREATION:
        return f"It was blocked from the day it was created, {age.entered_on}."
    return f"Status changed from {age.from_status} to blocked on {age.entered_on}."


def plan_promotion(snapshot: ProjectSnapshot, policy: PromotionPolicy, *, db_path) -> PromotionPlan:
    tracker = TrackerMock(db_path=db_path)
    as_of = parse_moment(snapshot.taken_at)
    ages: dict[str, BlockedAge] = {}
    stored: dict[str, str | None] = {}
    items = []
    for item in snapshot.items:
        if item.status == "blocked":
            age = compute_blocked_age(
                tracker.list_transitions(item.id), created_at=item.created_at, current_status=item.status,
                as_of=as_of, tz=snapshot.timezone,
            )
            ages[item.id] = age
            stored[item.id] = item.blocked_since
            if age.days is not None:
                item = item.model_copy(update={"blocked_since": age.entered_on})
        items.append(item)
    rebuilt = snapshot.model_copy(update={"items": items})

    plan = PromotionPlan(policy=policy, snapshot=rebuilt)
    plan.already_logged.extend(sorted(set(current_blockers(rebuilt)) - {g.item_id for g in find_gaps(rebuilt)}))
    for gap in find_gaps(rebuilt):
        age = ages[gap.item_id]
        if age.days is None:
            plan.age_unknown.append((gap, age))
            continue
        candidate = PromotionCandidate(replace(gap, transition_note=_transition_note(age)), age, stored[gap.item_id])
        (plan.eligible if age.days > policy.threshold_days else plan.below_threshold).append(candidate)
    return plan
