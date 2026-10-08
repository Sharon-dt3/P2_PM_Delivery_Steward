"""The weekly status report (PM-29): a client-facing draft, every figure computed in code from three STORED snapshots. The agent never sends it.

Three snapshots of the project, taken seven days apart, are the only inputs: `end` (now), `start` (a week before) and `previous` (a week before that).
Every number in the report is a difference or a count over those, so every number can be recomputed from them (pm.reporting.weekly_check, PM-30).

  progress          the current sprint's items done and by status, as of `end`; what was completed this week (done at `end`, not done at `start`)
  scope change      items that joined the sprint AFTER PLANNING (created more than SCOPE_GRACE_DAYS after the sprint started), and which joined this week
  top risks         open risks, high first, then the oldest
  decisions needed  blocked items, oldest first, with how long: each one needs someone to decide or unblock it. What to decide is not guessed
  velocity          completed this week against the week before, and what else changed alongside it. It says what coincided, never what caused it
  statuses          a status the tracker uses that is not in the enum is shown as UNMAPPED with the value it had, never filed under a nearby one

No model writes any of this: the wording is a fixed template over the facts, so the same snapshots always make the same report.
"""

from __future__ import annotations

import os
from datetime import date

from pydantic import BaseModel

from pm.reporting.facts import SprintScopeFacts
from pm.state.moments import parse_moment
from pm.state.snapshot import UNMAPPED, NormalizedItem, ProjectSnapshot

ENV_GRACE_DAYS = "PM_SCOPE_GRACE_DAYS"
DEFAULT_GRACE_DAYS = 2  # every seeded item but the two planted ones was created within 2 days of its sprint's start
TOP_RISKS = 5
_SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}
_STATUS_ORDER = ("done", "in_review", "in_progress", "blocked", "backlog", UNMAPPED)


def grace_days() -> int:
    raw = (os.environ.get(ENV_GRACE_DAYS) or "").strip()
    return int(raw) if raw.isdigit() else DEFAULT_GRACE_DAYS


class Figure(BaseModel):
    key: str
    value: int | float | str


class ItemLine(BaseModel):
    item_id: str
    title: str
    detail: str  # e.g. the date it was created, or since when it has been blocked


class RiskLine(BaseModel):
    risk_id: str
    severity: str
    title: str
    related_item_id: str | None = None


class WeeklyFacts(BaseModel):
    end_taken_at: str
    start_taken_at: str
    previous_taken_at: str
    week_ending: str  # the calendar date of `end`, in the project's timezone
    sprint: SprintScopeFacts | None = None
    grace_days: int = DEFAULT_GRACE_DAYS
    by_status: dict[str, int] = {}
    unmapped: list[ItemLine] = []  # detail = the raw status the tracker holds
    completed_this_week: list[str] = []
    completed_previous_week: list[str] = []
    velocity_change_percent: int | None = None  # None when the week before completed nothing: a change from zero is not a percentage
    added_after_planning: list[ItemLine] = []
    added_this_week: list[str] = []
    top_risks: list[RiskLine] = []
    blocked: list[ItemLine] = []  # oldest first; detail = "since 2026-09-14, 4 days"
    blocked_days: dict[str, int] = {}
    figures: list[Figure] = []


def _local_date(snapshot: ProjectSnapshot) -> date:
    from zoneinfo import ZoneInfo

    return parse_moment(snapshot.taken_at).astimezone(ZoneInfo(snapshot.timezone)).date()


def _sprint_of(snapshot: ProjectSnapshot) -> SprintScopeFacts | None:
    today = _local_date(snapshot)
    for sprint in snapshot.sprints:
        start, end = date.fromisoformat(sprint.start_date), date.fromisoformat(sprint.end_date)
        if start <= today <= end:
            members = [i for i in snapshot.items if i.sprint_id == sprint.id]
            return SprintScopeFacts(
                sprint_id=sprint.id, display_name=sprint.display_name, start_date=sprint.start_date, end_date=sprint.end_date,
                day_number=(today - start).days + 1, total_days=(end - start).days + 1,
                total_items=len(members), done_items=sum(1 for i in members if i.status == "done"),
            )
    return None


def _done(snapshot: ProjectSnapshot) -> set[str]:
    return {i.id for i in snapshot.items if i.status == "done"}


def compute_weekly_facts(end: ProjectSnapshot, start: ProjectSnapshot, previous: ProjectSnapshot) -> WeeklyFacts:
    """Everything the report says, from the three snapshots and nothing else."""
    sprint = _sprint_of(end)
    grace = grace_days()
    members: list[NormalizedItem] = [i for i in end.items if sprint and i.sprint_id == sprint.sprint_id]
    today = _local_date(end)

    by_status = {s: sum(1 for i in members if i.status == s) for s in _STATUS_ORDER}
    by_status = {s: n for s, n in by_status.items() if n}
    unmapped = [ItemLine(item_id=i.id, title=i.title, detail=i.raw_status) for i in sorted(members, key=lambda i: i.id) if i.status == UNMAPPED]

    done_end, done_start, done_previous = _done(end), _done(start), _done(previous)
    completed = sorted(done_end - done_start)
    completed_before = sorted(done_start - done_previous)
    change = round((len(completed) - len(completed_before)) / len(completed_before) * 100) if completed_before else None

    added_after_planning: list[ItemLine] = []
    if sprint:
        planned_until = date.fromisoformat(sprint.start_date).toordinal() + grace
        for item in sorted(members, key=lambda i: (i.created_at, i.id)):
            if date.fromisoformat(item.created_at[:10]).toordinal() > planned_until:
                added_after_planning.append(ItemLine(item_id=item.id, title=item.title, detail=f"created {item.created_at[:10]}"))
    in_start = {i.id for i in start.items}
    added_this_week = sorted(i.id for i in members if i.id not in in_start)

    open_risks = sorted((r for r in end.risks if r.status == "open"), key=lambda r: (_SEVERITY_RANK.get(r.severity, 3), r.opened_at, r.id))
    top_risks = [RiskLine(risk_id=r.id, severity=r.severity, title=r.title, related_item_id=r.related_item_id) for r in open_risks[:TOP_RISKS]]

    blocked_days: dict[str, int] = {}
    blocked: list[ItemLine] = []
    for item in sorted((i for i in members if i.status == "blocked"), key=lambda i: (i.blocked_since or "9999", i.id)):
        if item.blocked_since:
            days = (today - date.fromisoformat(item.blocked_since[:10])).days
            blocked_days[item.id] = days
            blocked.append(ItemLine(item_id=item.id, title=item.title, detail=f"since {item.blocked_since[:10]}, {days} day{'' if days == 1 else 's'}"))
        else:
            blocked.append(ItemLine(item_id=item.id, title=item.title, detail="blocked, with no date recorded"))

    facts = WeeklyFacts(
        end_taken_at=end.taken_at, start_taken_at=start.taken_at, previous_taken_at=previous.taken_at, week_ending=today.isoformat(),
        sprint=sprint, grace_days=grace, by_status=by_status, unmapped=unmapped, completed_this_week=completed,
        completed_previous_week=completed_before, velocity_change_percent=change, added_after_planning=added_after_planning,
        added_this_week=added_this_week, top_risks=top_risks, blocked=blocked, blocked_days=blocked_days,
    )
    return facts.model_copy(update={"figures": figures_of(facts)})


def figures_of(facts: WeeklyFacts) -> list[Figure]:
    """Every number the report states, by name. The rendered text uses these and no other numbers."""
    figures: list[Figure] = [Figure(key="grace_days", value=facts.grace_days)]
    if facts.sprint:
        s = facts.sprint
        figures += [Figure(key="sprint.day", value=s.day_number), Figure(key="sprint.total_days", value=s.total_days),
                    Figure(key="sprint.total_items", value=s.total_items), Figure(key="sprint.done_items", value=s.done_items)]
    figures += [Figure(key=f"status.{name}", value=n) for name, n in facts.by_status.items()]
    figures += [Figure(key="completed_this_week", value=len(facts.completed_this_week)),
                Figure(key="completed_previous_week", value=len(facts.completed_previous_week)),
                Figure(key="added_after_planning", value=len(facts.added_after_planning)),
                Figure(key="added_this_week", value=len(facts.added_this_week)),
                Figure(key="blocked", value=len(facts.blocked)), Figure(key="unmapped", value=len(facts.unmapped)),
                Figure(key="top_risks", value=len(facts.top_risks))]
    if facts.velocity_change_percent is not None:
        figures.append(Figure(key="velocity_change_percent", value=abs(facts.velocity_change_percent)))
    figures += [Figure(key=f"blocked_days.{item_id}", value=days) for item_id, days in facts.blocked_days.items()]
    return figures


def _items(ids: list[str]) -> str:
    return ", ".join(ids) if ids else "none"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def render_weekly_report(facts: WeeklyFacts) -> str:
    """The report as text: a fixed template over the facts. Nothing is worded by a model."""
    out = [f"Weekly status report, week ending {facts.week_ending} (a draft for a person to read; it is never sent).",
           "Every figure is computed from three stored project snapshots and can be recomputed from them; none is estimated.", ""]

    out.append("## Progress against sprint scope")
    if facts.sprint:
        s = facts.sprint
        out.append(f"- {s.display_name} ({s.sprint_id}), day {s.day_number} of {s.total_days}: {s.done_items} of {_plural(s.total_items, 'item')} done.")
        if facts.by_status:
            out.append("- By status: " + ", ".join(f"{name} {n}" for name, n in facts.by_status.items()) + ".")
    else:
        out.append("- No sprint on file covers this date.")
    out.append(f"- Completed this week: {_plural(len(facts.completed_this_week), 'item')} ({_items(facts.completed_this_week)}).")
    for u in facts.unmapped:
        out.append(f"- {u.item_id} has a status the tracker does not map: UNMAPPED (the tracker says {u.detail!r}). It is not counted as pending or blocked.")

    out += ["", "## Scope change"]
    added = facts.added_after_planning
    if added:
        out.append(f"- Added after planning (created more than {_plural(facts.grace_days, 'day')} after the sprint started): {len(added)}.")
        out += [f"  - {a.item_id} ({a.detail}): {a.title}" for a in added]
    else:
        out.append("- No item was added after planning.")
    out.append(f"- Joined the sprint this week: {_plural(len(facts.added_this_week), 'item')} ({_items(facts.added_this_week)}).")

    out += ["", "## Top risks"]
    if facts.top_risks:
        for rank, r in enumerate(facts.top_risks, start=1):
            about = f" (item {r.related_item_id})" if r.related_item_id else ""
            out.append(f"{rank}. [{r.severity}] {r.risk_id}: {r.title}{about}")
    else:
        out.append("- No open risk is recorded.")

    out += ["", "## Decisions needed from the client"]
    if facts.blocked:
        out.append("Each blocked item needs someone to decide or to unblock it, oldest first. What to decide is not guessed here.")
        out += [f"- {b.item_id} blocked {b.detail}: {b.title}" for b in facts.blocked]
    else:
        out.append("- Nothing is blocked.")

    out += ["", "## Velocity"]
    this, before = len(facts.completed_this_week), len(facts.completed_previous_week)
    if facts.velocity_change_percent is None:
        out.append(f"- Completed this week: {this}. The week before: {before}, so there is no percentage change to state.")
    else:
        pct = facts.velocity_change_percent
        direction = "up" if pct > 0 else "down" if pct < 0 else "unchanged"
        change = "unchanged" if pct == 0 else f"{direction} {abs(pct)}%"
        out.append(f"- Completed this week: {this}, against {before} the week before: {change}.")
    out.append(f"- Alongside it: {_plural(len(facts.added_this_week), 'item')} joined the sprint ({_items(facts.added_this_week)}) and "
               f"{len(facts.blocked)} {'is' if len(facts.blocked) == 1 else 'are'} blocked ({_items([b.item_id for b in facts.blocked])}). "
               "That is what changed in the same week; it does not say what caused the change.")
    return "\n".join(out)
