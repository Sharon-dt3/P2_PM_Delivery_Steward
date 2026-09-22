"""
Morning brief facts (PM-08, extended by PM-10).

Facts in code, prose from the model -- the same split P1 itself proved
for its own daily/weekly summaries (src/p1/reporting/facts.py +
daily_summary.py), applied to this repo's own snapshot abstraction rather
than a live DB read: compute_morning_brief_facts() is a PURE function of
one ProjectSnapshot (PM-05, extended by PM-08 to also carry sprints/
commitments/risks, and by PM-10 to carry the roster) and nothing else --
no adapter call, no clock read, no randomness. That purity is what the
row's own acceptance test asks for literally: "Regenerating on the same
snapshot yields identical facts; wording may differ." Calling this
function twice on the same snapshot object always returns two equal
MorningBriefFacts instances (see tests/unit/test_reporting_facts.py);
only pm.reporting.morning_brief's own model call downstream of these
facts is allowed to vary.

This module has zero import of spine.llm or spine.prompts anywhere in
it, mirroring P1's own p1/reporting/facts.py convention exactly (see that
module's own docstring) -- physically separating "what happened" from
"how it's phrased" is what makes a fact computation trustworthy on its
own terms, independent of anything the model might do with it.

The four per-person buckets map onto two different data sources
deliberately, not by accident:
  - committed  -- this person's own rows in snapshot.commitments (what
                  they SAID they'd do; independent of tracker status).
  - delivered/pending/blocked -- this person's own tracker items,
                  bucketed by NormalizedItem.status (what's ACTUALLY
                  true right now): done -> delivered, blocked ->
                  blocked, anything else (backlog/in_progress/in_review/
                  UNMAPPED) -> pending.
Blockers are every OPEN risk in snapshot.risks, ranked by severity (high,
then medium, then low -- RISK-003's own "mitigated" status is exactly why
this filters on status=="open", not just "every risk on file"), tied
broken by risk id for a fully deterministic order.

PM-10 ("A person with no activity is reported as having no activity --
not omitted, not embellished") changes _compute_person_facts's own base
set: it used to be built ONLY from ids actually observed on an item or a
commitment, so a person who owned neither -- a real possibility, not a
hypothetical -- was silently absent from `people` altogether, not merely
shown with empty buckets. The base set is now snapshot.roster itself
(every person PM-10's row says must be accounted for), unioned
defensively with any id observed on an item/commitment that isn't on the
roster (so a data gap in the roster can never make this module drop real
activity, mirroring P1's own participation ledger only ever narrowing
roster - contributors, never inventing membership). commit_count is new
too -- snapshot.commits carries activity no item or commitment bucket
would otherwise surface (the row's own "tracker AND commit activity") --
and PersonFacts.has_activity (a plain property, not a stored field, so it
can never itself drift out of sync with the buckets it is computed from)
is what pm.reporting.morning_brief checks to decide whether a person gets
the four normal buckets or PM-10's own explicit no-update line.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel

from pm.adapters.commitments import Commitment
from pm.state.snapshot import ProjectSnapshot

_SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}


class ItemFact(BaseModel):
    item_id: str
    title: str


class PersonFacts(BaseModel):
    assignee_id: str
    committed: list[Commitment]
    delivered: list[ItemFact]
    pending: list[ItemFact]
    blocked: list[ItemFact]
    commit_count: int = 0  # commits authored in snapshot.commits, regardless of item_ref (PM-10)

    @property
    def has_activity(self) -> bool:
        """PM-10's own zero/non-zero determination, computed once here
        (never re-derived, possibly inconsistently, by a renderer) from
        exactly the facts this model already carries. A plain Python
        property, not a pydantic field, so it never participates in this
        model's own equality/serialization -- it can't drift from the
        buckets it reads because it never has a stored value of its own
        to drift from."""
        return bool(self.committed or self.delivered or self.pending or self.blocked or self.commit_count)


class BlockerFact(BaseModel):
    risk_id: str
    title: str
    severity: str  # low | medium | high
    related_item_id: str | None = None
    assignee_id: str | None = None  # resolved from related_item_id, when it names a real item


class SprintScopeFacts(BaseModel):
    sprint_id: str
    display_name: str
    start_date: str
    end_date: str
    day_number: int  # 1-indexed day within [start_date, end_date], as of the snapshot's own taken_at
    total_days: int  # inclusive day count of the sprint
    total_items: int
    done_items: int


class MorningBriefFacts(BaseModel):
    as_of: str  # the snapshot's own taken_at, carried through unchanged
    sprint: SprintScopeFacts | None = None  # None if taken_at's date falls outside every known sprint
    people: list[PersonFacts]
    blockers: list[BlockerFact]


def _resolve_sprint_scope(snapshot: ProjectSnapshot, as_of_date: str) -> SprintScopeFacts | None:
    for sprint in snapshot.sprints:
        if sprint.start_date <= as_of_date <= sprint.end_date:
            start = date.fromisoformat(sprint.start_date)
            end = date.fromisoformat(sprint.end_date)
            today = date.fromisoformat(as_of_date)
            in_sprint = [item for item in snapshot.items if item.sprint_id == sprint.id]
            done = [item for item in in_sprint if item.status == "done"]
            return SprintScopeFacts(
                sprint_id=sprint.id,
                display_name=sprint.display_name,
                start_date=sprint.start_date,
                end_date=sprint.end_date,
                day_number=(today - start).days + 1,
                total_days=(end - start).days + 1,
                total_items=len(in_sprint),
                done_items=len(done),
            )
    return None  # as_of_date isn't covered by any sprint on file -- an honest gap, not an error


def _compute_person_facts(snapshot: ProjectSnapshot) -> list[PersonFacts]:
    # The roster is the base set (PM-10): every person snapshot.roster
    # names gets a PersonFacts entry, whether or not they own a single
    # item, commitment or commit. Ids observed on an item/commitment but
    # missing from the roster are unioned in defensively -- a gap in the
    # roster must never cause this function to drop real, already-known
    # activity; it should only ever ADD zero-activity people, never
    # remove anyone the old items-and-commitments-only view would have
    # found.
    assignee_ids = {assignee.id for assignee in snapshot.roster}
    assignee_ids |= {item.assignee_id for item in snapshot.items if item.assignee_id is not None}
    assignee_ids |= {commitment.member_id for commitment in snapshot.commitments}

    people: list[PersonFacts] = []
    for assignee_id in sorted(assignee_ids):
        their_items = [item for item in snapshot.items if item.assignee_id == assignee_id]
        their_commitments = sorted(
            (c for c in snapshot.commitments if c.member_id == assignee_id),
            key=lambda c: (c.due_date_iso or c.due_date_text or "", c.id),
        )
        delivered = sorted(
            (ItemFact(item_id=item.id, title=item.title) for item in their_items if item.status == "done"),
            key=lambda fact: fact.item_id,
        )
        blocked = sorted(
            (ItemFact(item_id=item.id, title=item.title) for item in their_items if item.status == "blocked"),
            key=lambda fact: fact.item_id,
        )
        pending = sorted(
            (
                ItemFact(item_id=item.id, title=item.title)
                for item in their_items
                if item.status not in ("done", "blocked")
            ),
            key=lambda fact: fact.item_id,
        )
        commit_count = sum(1 for commit in snapshot.commits if commit.author_id == assignee_id)
        people.append(
            PersonFacts(
                assignee_id=assignee_id,
                committed=their_commitments,
                delivered=delivered,
                pending=pending,
                blocked=blocked,
                commit_count=commit_count,
            )
        )
    return people


def _compute_blocker_facts(snapshot: ProjectSnapshot) -> list[BlockerFact]:
    items_by_id = {item.id: item for item in snapshot.items}
    open_risks = [risk for risk in snapshot.risks if risk.status == "open"]

    blockers = []
    for risk in open_risks:
        related_item = items_by_id.get(risk.related_item_id) if risk.related_item_id else None
        blockers.append(
            BlockerFact(
                risk_id=risk.id,
                title=risk.title,
                severity=risk.severity,
                related_item_id=risk.related_item_id,
                assignee_id=related_item.assignee_id if related_item is not None else None,
            )
        )
    blockers.sort(key=lambda blocker: (_SEVERITY_RANK.get(blocker.severity, len(_SEVERITY_RANK)), blocker.risk_id))
    return blockers


def compute_morning_brief_facts(snapshot: ProjectSnapshot) -> MorningBriefFacts:
    """The one entry point: every morning-brief fact, derived purely from
    `snapshot`. Calling this twice on the same snapshot object always
    returns two equal MorningBriefFacts -- there is nothing in this
    function that could make it do otherwise (no clock read, no
    randomness, no I/O)."""
    as_of_date = snapshot.taken_at[:10]
    return MorningBriefFacts(
        as_of=snapshot.taken_at,
        sprint=_resolve_sprint_scope(snapshot, as_of_date),
        people=_compute_person_facts(snapshot),
        blockers=_compute_blocker_facts(snapshot),
    )
