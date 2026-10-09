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
                  blocked, backlog/in_progress/in_review -> pending.
  - unmapped  -- a tracker item whose status is not one of the tracker's
                  own (PM-31): NormalizedItem.status is UNMAPPED and its
                  raw_status is carried here untouched. It is never filed
                  under pending (or anywhere else): silent coercion is
                  guessing with extra steps, so it is shown as what it is.
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
defensively with any id observed on an item, commitment or commit that
isn't on the roster (so a data gap in the roster can never make this module
drop real activity, mirroring P1's own participation ledger only ever
narrowing roster - contributors, never inventing membership). commit_count is new
too -- snapshot.commits carries activity no item or commitment bucket
would otherwise surface (the row's own "tracker AND commit activity") --
and PersonFacts.has_activity (a plain property, not a stored field, so it
can never itself drift out of sync with the buckets it is computed from)
is what pm.reporting.morning_brief checks to decide whether a person gets
the four normal buckets or PM-10's own explicit no-update line.
"""

from __future__ import annotations

from datetime import date
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from pm.adapters.commitments import Commitment
from pm.people import AMBIGUOUS, IGNORED, PERSON, UNKNOWN, Person, name_key
from pm.people import labels as people_labels
from pm.state.moments import parse_moment
from pm.state.snapshot import UNMAPPED, ProjectSnapshot

_SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}


class ItemFact(BaseModel):
    item_id: str
    title: str


class UnmappedFact(BaseModel):
    item_id: str
    title: str
    raw_status: str  # the tracker's own value, exactly as it holds it


class UnassignedFact(BaseModel):
    item_id: str
    title: str
    status: str
    raw_status: str = ""  # the tracker's own value; carried so that an UNMAPPED status is never shown without it


class AuthorNote(BaseModel):
    """A commit author the brief could not attribute to one person (PM-32), said as what it is instead of guessed."""

    kind: str  # "ambiguous": could be more than one person | "unmatched": resembles one person but is not their id, name or an alias
    author: str  # exactly as the code host spells it
    candidates: list[str]  # the people it could be, by the labels the brief uses
    commits: int


class UnreferencedCommit(BaseModel):
    sha: str
    subject: str  # the first line of the commit message, exactly as written
    author: str  # the roster name when the author is a known person, else the author as written
    committed_on: str  # the calendar date, as recorded


class PersonFacts(BaseModel):
    assignee_id: str
    display_name: str | None = None  # from the tracker's roster; None when it has none for this id
    committed: list[Commitment]
    delivered: list[ItemFact]
    pending: list[ItemFact]
    blocked: list[ItemFact]
    unmapped: list[UnmappedFact] = []  # items whose status the tracker does not map: shown with the raw value, never counted as pending
    commit_count: int = 0  # commits authored in snapshot.commits, regardless of item_ref (PM-10)

    @property
    def name(self) -> str:
        """What the brief calls this person: their name, or their id if no name
        is known. The id is still what grounding and the audit trail use."""
        return self.display_name or self.assignee_id

    @property
    def has_activity(self) -> bool:
        """PM-10's own zero/non-zero determination, computed once here
        (never re-derived, possibly inconsistently, by a renderer) from
        exactly the facts this model already carries. A plain Python
        property, not a pydantic field, so it never participates in this
        model's own equality/serialization -- it can't drift from the
        buckets it reads because it never has a stored value of its own
        to drift from."""
        return bool(self.committed or self.delivered or self.pending or self.blocked or self.unmapped or self.commit_count)


class BlockerFact(BaseModel):
    risk_id: str
    title: str
    severity: str  # low | medium | high
    related_item_id: str | None = None
    assignee_id: str | None = None  # resolved from related_item_id, when it names a real item
    assignee_name: str | None = None  # that person's name, when the roster has one


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
    author_notes: list[AuthorNote] = []  # authors that are ambiguous or unmatched: counted for nobody, and said so
    unreferenced_commits: list[UnreferencedCommit] = []  # commits by people that name no work item: counted as activity, but not tied to any work
    unassigned: list[UnassignedFact] = []  # open items nobody owns: they belong to no person's section, so they would otherwise appear nowhere
    automated_commit_count: int = 0  # commits by ignored automated accounts (bots): not anyone's activity, but not hidden


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


def _roster_people(snapshot: ProjectSnapshot) -> list[Person]:
    return [Person(id=assignee.id, name=assignee.display_name) for assignee in snapshot.roster]


def _commits_by_person(snapshot: ProjectSnapshot) -> tuple[dict[str, int], int, list[AuthorNote]]:
    """Commits per canonical person, the number by ignored automated accounts, and the authors that could not be attributed to one person.

    Authors are resolved by the one rule in pm.people (PM-32): an exact alias, id or display name is that person; an author that is nobody's is
    taken as written (a typo shows up as a new name instead of being merged into the wrong person, and says who it resembles); an author that could be
    more than one person (a bare first name, an initial) is counted for NOBODY and listed as ambiguous."""
    people = _roster_people(snapshot)
    snapshot.identities.validate([p.id for p in people])
    labels = people_labels(people)
    counts: dict[str, int] = {}
    automated = 0
    notes: dict[str, AuthorNote] = {}
    for commit in snapshot.commits:
        answer = snapshot.identities.resolution(commit.author_id, people)
        if answer.kind == IGNORED:
            automated += 1
        elif answer.kind == PERSON:
            counts[answer.person_id] = counts.get(answer.person_id, 0) + 1
        else:
            counts_as = commit.author_id.strip()
            if answer.kind == UNKNOWN:
                counts[counts_as] = counts.get(counts_as, 0) + 1  # its own name, never someone else's
            if answer.candidates:
                key = f"{answer.kind}:{name_key(commit.author_id)}"
                note = notes.get(key) or AuthorNote(
                    kind="ambiguous" if answer.kind == AMBIGUOUS else "unmatched", author=commit.author_id.strip(),
                    candidates=[labels.get(c, c) for c in answer.candidates], commits=0)
                notes[key] = note.model_copy(update={"commits": note.commits + 1})
    return counts, automated, sorted(notes.values(), key=lambda n: (n.kind, n.author.lower()))


def _unreferenced_commits(snapshot: ProjectSnapshot, names: dict[str, str]) -> list[UnreferencedCommit]:
    """Commits by real people (not ignored automated accounts) whose message names no work item, oldest first."""
    people = _roster_people(snapshot)
    found = []
    for commit in snapshot.commits:
        if commit.item_ref is not None:
            continue
        answer = snapshot.identities.resolution(commit.author_id, people)
        if answer.kind == IGNORED:
            continue
        if answer.kind == PERSON:
            author = names.get(answer.person_id, answer.person_id)
        elif answer.kind == AMBIGUOUS:  # not credited to anyone: shown as written, and as ambiguous
            author = f"{commit.author_id.strip()} (ambiguous: could be {' or '.join(names.get(c, c) for c in answer.candidates)})"
        else:
            author = commit.author_id.strip()
        found.append(UnreferencedCommit(
            sha=commit.sha, subject=commit.message.splitlines()[0] if commit.message else "",
            author=author, committed_on=commit.committed_at[:10],
        ))
    return sorted(found, key=lambda c: (c.committed_on, c.sha))


def _person_names(snapshot: ProjectSnapshot) -> dict[str, str]:
    """Roster names by id. Two people with the same name are told apart by
    adding their id ("Sam Lee (sam.one)") so a heading is never ambiguous."""
    return people_labels(_roster_people(snapshot))  # "the same name" is the one rule's: ignoring case and outer spaces, as P1 matches names


def _compute_person_facts(
    snapshot: ProjectSnapshot, commit_counts: dict[str, int], names: dict[str, str]
) -> list[PersonFacts]:
    # The roster is the base set (PM-10): every person snapshot.roster
    # names gets a PersonFacts entry, whether or not they own a single
    # item, commitment or commit. Ids observed on an item/commitment/commit but
    # missing from the roster are unioned in defensively -- a gap in the
    # roster must never cause this function to drop real, already-known
    # activity; it should only ever ADD zero-activity people, never
    # remove anyone the old items-and-commitments-only view would have
    # found.
    assignee_ids = {assignee.id for assignee in snapshot.roster}
    assignee_ids |= {item.assignee_id for item in snapshot.items if item.assignee_id is not None}
    assignee_ids |= {commitment.member_id for commitment in snapshot.commitments}
    assignee_ids |= set(commit_counts)

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
                if item.status not in ("done", "blocked", UNMAPPED)
            ),
            key=lambda fact: fact.item_id,
        )
        unmapped = sorted(
            (UnmappedFact(item_id=item.id, title=item.title, raw_status=item.raw_status) for item in their_items if item.status == UNMAPPED),
            key=lambda fact: fact.item_id,
        )
        commit_count = commit_counts.get(assignee_id, 0)
        people.append(
            PersonFacts(
                assignee_id=assignee_id,
                display_name=names.get(assignee_id),
                committed=their_commitments,
                delivered=delivered,
                pending=pending,
                blocked=blocked,
                unmapped=unmapped,
                commit_count=commit_count,
            )
        )
    return people


def _compute_blocker_facts(snapshot: ProjectSnapshot, names: dict[str, str]) -> list[BlockerFact]:
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
                assignee_name=names.get(related_item.assignee_id) if related_item is not None else None,
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
    # The calendar date in the project's own timezone: sprint dates are local
    # dates, and the UTC date of taken_at is a different day for a team far from UTC.
    as_of_date = parse_moment(snapshot.taken_at).astimezone(ZoneInfo(snapshot.timezone)).date().isoformat()
    commit_counts, automated, author_notes = _commits_by_person(snapshot)
    names = _person_names(snapshot)
    return MorningBriefFacts(
        as_of=snapshot.taken_at,
        sprint=_resolve_sprint_scope(snapshot, as_of_date),
        people=_compute_person_facts(snapshot, commit_counts, names),
        blockers=_compute_blocker_facts(snapshot, names),
        unreferenced_commits=_unreferenced_commits(snapshot, names),
        unassigned=sorted(
            (UnassignedFact(item_id=i.id, title=i.title, status=i.status, raw_status=i.raw_status) for i in snapshot.items
             if i.assignee_id is None and i.status != "done"),
            key=lambda fact: fact.item_id,
        ),
        author_notes=author_notes,
        automated_commit_count=automated,
    )
