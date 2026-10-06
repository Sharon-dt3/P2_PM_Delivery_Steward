"""The gap set: current blockers with NO entry in the risk log (PM-16). Pure Python.

A blocker is an item whose tracker status is `blocked` at the snapshot's moment. It is
covered if ANY risk-log entry, in any status, names that item (a person already
decided it belongs in the log; a mitigated or closed entry is still an entry). The gap
set is the blockers nobody has logged, sorted by item id.

Every number in the evidence -- days blocked, days left in the sprint, days a
commitment is overdue -- is computed here from dates in the data. A model is only ever
asked to phrase them (pm.risk.proposals), and is held to exactly these numbers.

The suggested owner is the item's tracker assignee, and nothing else: a commitment by
someone, a commit author or a message author is not evidence of ownership.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from zoneinfo import ZoneInfo

from pm.reporting.facts import _person_names
from pm.state.moments import parse_moment
from pm.state.snapshot import ProjectSnapshot


def _days(n: int) -> str:
    return f"{n} day" if n == 1 else f"{n} days"


@dataclass(frozen=True)
class SuggestedOwner:
    id: str
    name: str
    evidence: str


@dataclass(frozen=True)
class CommitmentEvidence:
    id: int
    member_id: str
    member_name: str
    text: str
    due_date_iso: str | None
    due_date_text: str | None
    days_overdue: int | None  # set only when the due date has passed
    days_until_due: int | None  # set only when it has not


@dataclass(frozen=True)
class BlockerGap:
    item_id: str
    title: str
    as_of: str
    assignee_id: str | None
    assignee_name: str | None
    sprint_id: str
    sprint_name: str | None
    sprint_end: str | None
    sprint_days_left: int | None
    blocked_since: str | None
    days_blocked: int | None
    commitments: tuple[CommitmentEvidence, ...]
    commits: tuple[tuple[str, str], ...]  # (sha, message)
    source_message: tuple[str, str] | None  # (message id, body)

    @property
    def reference(self) -> str:
        return f"item:{self.item_id}"

    @property
    def owner(self) -> SuggestedOwner | None:
        if not self.assignee_id:
            return None
        return SuggestedOwner(
            id=self.assignee_id, name=self.assignee_name or self.assignee_id,
            evidence=f"assignee of {self.item_id} in the tracker",
        )

    # --- the evidence, one sentence per fact ---------------------------------------------------------------

    def blocked_sentence(self) -> str | None:
        if not self.blocked_since:
            return None
        if self.days_blocked is None:
            return f"Blocked since {self.blocked_since}."
        return f"Blocked since {self.blocked_since}, {_days(self.days_blocked)} as of {self.as_of}."

    def sprint_sentence(self) -> str | None:
        if not self.sprint_end:
            return None
        left = f", {_days(self.sprint_days_left)} left" if self.sprint_days_left is not None else ""
        return f"{self.sprint_name or self.sprint_id} ({self.sprint_id}) ends {self.sprint_end}{left}."

    def commitment_sentences(self) -> list[str]:
        out = []
        for c in self.commitments:
            who = c.member_name if c.member_name == c.member_id else f"{c.member_name}"
            sentence = f'Commitment by {who}: "{c.text}"'
            if c.due_date_iso:
                sentence += f" due {c.due_date_iso}"
                if c.days_overdue:
                    sentence += f", {_days(c.days_overdue)} overdue"
                elif c.days_until_due is not None:
                    sentence += f", due in {_days(c.days_until_due)}"
            elif c.due_date_text:
                sentence += f" due {c.due_date_text}"
            out.append(sentence + ".")
        return out

    def commit_sentences(self) -> list[str]:
        return [f'Commit {sha}: "{message}".' for sha, message in self.commits]

    def message_sentence(self) -> str | None:
        if not self.source_message:
            return None
        return f'Teams message {self.source_message[0]}: "{self.source_message[1]}".'

    def owner_sentence(self) -> str | None:
        if not self.assignee_id:
            return None
        label = self.assignee_name if self.assignee_name and self.assignee_name != self.assignee_id else None
        return f"Assignee: {label} ({self.assignee_id})." if label else f"Assignee: {self.assignee_id}."

    def evidence_text(self) -> str:
        """Everything known about this blocker as one line of text. It is what the
        model is shown and the only thing its words are checked against."""
        parts = [f'{self.item_id} "{self.title}" is blocked.', self.owner_sentence(), self.blocked_sentence(), self.sprint_sentence(),
                 *self.commitment_sentences(), *self.commit_sentences(), self.message_sentence()]
        return " ".join(p for p in parts if p)

    # --- the templated prose: what a faithful model would say, used when it cannot be trusted --------------------

    def description_text(self) -> str:
        since = f" Blocked since {self.blocked_since}." if self.blocked_since else ""
        return f'{self.item_id} "{self.title}" is blocked.{since}'

    def description_quote(self) -> str:
        return f'"{self.title}"'

    def impact_text(self) -> str:
        parts = []
        if self.days_blocked is not None:
            parts.append(f"It has been blocked for {_days(self.days_blocked)} as of {self.as_of}.")
        if self.sprint_end:
            left = f", {_days(self.sprint_days_left)} left" if self.sprint_days_left is not None else ""
            parts.append(f"{self.sprint_name or self.sprint_id} ends {self.sprint_end}{left}.")
        for c in self.commitments:
            if not c.due_date_iso:
                continue
            when = f"{_days(c.days_overdue)} overdue" if c.days_overdue else f"in {_days(c.days_until_due)}"
            parts.append(f"Commitment due {c.due_date_iso}, {when}.")
        return " ".join(parts) or "No sprint end, due date or blocked date is recorded for it."

    def impact_quote(self) -> str:
        return self.blocked_sentence() or self.sprint_sentence() or self.description_quote()

    def allowed_durations(self) -> set[int]:
        """The only 'N days' figures a description or impact may state: the computed ones."""
        numbers = {self.days_blocked, self.sprint_days_left}
        for c in self.commitments:
            numbers.update({c.days_overdue, c.days_until_due})
        return {n for n in numbers if n is not None}

    def evidence_refs(self) -> list[str]:
        refs = [self.reference]
        if self.source_message:
            refs.append(f"message:{self.source_message[0]}")
        refs += [f"commit:{sha}" for sha, _ in self.commits]
        refs += [f"commitment:{c.id}" for c in self.commitments]
        return refs


def current_blockers(snapshot: ProjectSnapshot) -> list[str]:
    return sorted(item.id for item in snapshot.items if item.status == "blocked")


def covered_items(snapshot: ProjectSnapshot) -> set[str]:
    """Items that have a risk-log entry, in any status."""
    return {risk.related_item_id for risk in snapshot.risks if risk.related_item_id}


def _as_of(snapshot: ProjectSnapshot) -> date:
    return parse_moment(snapshot.taken_at).astimezone(ZoneInfo(snapshot.timezone)).date()


def find_gaps(snapshot: ProjectSnapshot) -> list[BlockerGap]:
    as_of = _as_of(snapshot)
    names = _person_names(snapshot)
    sprints = {s.id: s for s in snapshot.sprints}
    messages = {m.id: m for m in snapshot.channel.messages}
    covered = covered_items(snapshot)

    gaps = []
    for item in sorted((i for i in snapshot.items if i.status == "blocked" and i.id not in covered), key=lambda i: i.id):
        since = date.fromisoformat(item.blocked_since[:10]) if item.blocked_since else None
        sprint = sprints.get(item.sprint_id)
        end = date.fromisoformat(sprint.end_date) if sprint else None
        commitments = []
        for c in sorted((c for c in snapshot.commitments if c.item_id == item.id), key=lambda c: c.id):
            due = date.fromisoformat(c.due_date_iso) if c.due_date_iso else None
            commitments.append(CommitmentEvidence(
                id=c.id, member_id=c.member_id, member_name=names.get(c.member_id, c.member_id), text=c.text,
                due_date_iso=c.due_date_iso, due_date_text=c.due_date_text,
                days_overdue=(as_of - due).days if due and due < as_of else None,
                days_until_due=(due - as_of).days if due and due >= as_of else None,
            ))
        message = messages.get(item.source_message_id) if item.source_message_id else None
        gaps.append(BlockerGap(
            item_id=item.id, title=item.title, as_of=as_of.isoformat(), assignee_id=item.assignee_id,
            assignee_name=names.get(item.assignee_id, item.assignee_id) if item.assignee_id else None,
            sprint_id=item.sprint_id, sprint_name=sprint.display_name if sprint else None,
            sprint_end=end.isoformat() if end else None,
            sprint_days_left=(end - as_of).days if end and end >= as_of else None,
            blocked_since=since.isoformat() if since else None,
            days_blocked=(as_of - since).days if since and since <= as_of else None,
            commitments=tuple(commitments),
            commits=tuple((c.sha, c.message) for c in snapshot.commits if c.item_ref == item.id),
            source_message=(message.id, message.body) if message else None,
        ))
    return gaps
