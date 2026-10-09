"""PM-30: every number in the weekly report can be recomputed from the stored snapshots.

`recompute_figures` re-derives each figure from the three stored snapshots by its own, deliberately plain code (loops over the items, not the sets and
helpers the report is built with), so a mistake in one does not hide in the other. `check_report` then says what disagrees:

  figures      each figure the report states, recomputed, against the value the report holds
  text         every number in the report's text, once ids, dates, the sprint's name and rank markers are set aside, is one of those figures:
               a number that is in the text and nowhere in the figures is a number that was never computed

It returns the problems as sentences; an empty list means the report is exactly what its snapshots say.
"""

from __future__ import annotations

import re
from datetime import date
from zoneinfo import ZoneInfo

from pm.reporting.weekly import _SEVERITY_RANK, TOP_RISKS, WeeklyFacts, grace_days
from pm.state.moments import parse_moment
from pm.state.snapshot import UNMAPPED, ProjectSnapshot


def _day(snapshot: ProjectSnapshot) -> date:
    return parse_moment(snapshot.taken_at).astimezone(ZoneInfo(snapshot.timezone)).date()


def recompute_figures(end: ProjectSnapshot, start: ProjectSnapshot, previous: ProjectSnapshot) -> dict[str, int]:
    today = _day(end)
    figures: dict[str, int] = {"grace_days": grace_days()}

    sprint = None
    for candidate in end.sprints:
        if date.fromisoformat(candidate.start_date) <= today <= date.fromisoformat(candidate.end_date):
            sprint = candidate
    members = [item for item in end.items if sprint is not None and item.sprint_id == sprint.id]
    if sprint is not None:
        figures["sprint.day"] = (today - date.fromisoformat(sprint.start_date)).days + 1
        figures["sprint.total_days"] = (date.fromisoformat(sprint.end_date) - date.fromisoformat(sprint.start_date)).days + 1
        figures["sprint.total_items"] = len(members)
        figures["sprint.done_items"] = len([item for item in members if item.status == "done"])
    for item in members:
        key = f"status.{item.status}"
        figures[key] = figures.get(key, 0) + 1

    def done_at(snapshot: ProjectSnapshot) -> dict[str, bool]:
        return {item.id: item.status == "done" for item in snapshot.items}

    now_done, then_done, before_done = done_at(end), done_at(start), done_at(previous)
    this_week = [i for i, d in now_done.items() if d and not then_done.get(i, False)]
    week_before = [i for i, d in then_done.items() if d and not before_done.get(i, False)]
    figures["completed_this_week"] = len(this_week)
    figures["completed_previous_week"] = len(week_before)
    if week_before:
        figures["velocity_change_percent"] = abs(round((len(this_week) - len(week_before)) / len(week_before) * 100))

    late = 0
    if sprint is not None:
        for item in members:
            if (date.fromisoformat(item.created_at[:10]) - date.fromisoformat(sprint.start_date)).days > grace_days():
                late += 1
    figures["added_after_planning"] = late
    existed_at_start = {item.id for item in start.items}
    figures["added_this_week"] = len([item for item in members if item.id not in existed_at_start])

    blocked = [item for item in members if item.status == "blocked"]
    figures["blocked"] = len(blocked)
    for item in blocked:
        if item.blocked_since:
            figures[f"blocked_days.{item.id}"] = (today - date.fromisoformat(item.blocked_since[:10])).days
    figures["unmapped"] = len([item for item in members if item.status == UNMAPPED])
    open_risks = [risk for risk in end.risks if risk.status == "open"]
    figures["top_risks"] = min(len(open_risks), TOP_RISKS)
    return figures


def check_report(facts: WeeklyFacts, text: str, end: ProjectSnapshot, start: ProjectSnapshot, previous: ProjectSnapshot) -> list[str]:
    problems: list[str] = []
    fresh = recompute_figures(end, start, previous)
    stated = {f.key: f.value for f in facts.figures}
    for key in sorted(set(fresh) | set(stated)):
        if key not in stated:
            problems.append(f"{key}: recomputes to {fresh[key]} but the report does not state it")
        elif key not in fresh:
            problems.append(f"{key}: the report states {stated[key]} but the snapshots do not produce it")
        elif stated[key] != fresh[key]:
            problems.append(f"{key}: the report states {stated[key]} but the snapshots give {fresh[key]}")

    # the top risks are in the order and at the severity the snapshots hold
    open_risks = sorted((r for r in end.risks if r.status == "open"), key=lambda r: (_SEVERITY_RANK.get(r.severity, 3), r.opened_at, r.id))
    if [r.risk_id for r in facts.top_risks] != [r.id for r in open_risks[:TOP_RISKS]]:
        problems.append("the top risks are not the open risks in severity order")

    problems += [f"the text states {n}, which is not one of the report's figures" for n in sorted(unexplained_numbers(facts, text))]
    return problems


def _recorded_text(facts: WeeklyFacts) -> list[str]:
    """The free text the report quotes from the tracker and the risk log, longest first so a title is removed whole before any part of it."""
    found = [line.title for line in (*facts.added_after_planning, *facts.blocked, *facts.unmapped)]
    found += [u.detail for u in facts.unmapped] + [risk.title for risk in facts.top_risks]
    return sorted({w for w in found if w}, key=len, reverse=True)


def unexplained_numbers(facts: WeeklyFacts, text: str) -> set[int]:
    """Numbers in the text that are not figures: set aside the free text quoted from the tracker and the risk log (titles, raw statuses), ids
    (PM-031, RISK-002, sprint-13), dates, the sprint's own name and rank markers first."""
    stripped = text
    for words in _recorded_text(facts):  # a title or a raw status is what someone wrote in the tracker or the risk log, not something computed: "P1 uses graph ids"
        stripped = re.sub(re.escape(words), " ", stripped, flags=re.IGNORECASE)
    stripped = re.sub(r"\b(?:PM|RISK|sprint)-\d+\b", " ", stripped)
    stripped = re.sub(r"\d{4}-\d{2}-\d{2}", " ", stripped)
    if facts.sprint:
        stripped = stripped.replace(facts.sprint.display_name, " ")
    stripped = re.sub(r"(?m)^\d+\. ", " ", stripped)
    allowed = {int(f.value) for f in facts.figures if isinstance(f.value, (int, float))}
    # A model line may quote a recorded title in plainer case or in part ("... a recurring 403 Forbidden error"), so a number that is written inside a recorded
    # title is that title's own number wherever it is quoted. A model line is already grounded against its own fact, so it cannot bring a number from another.
    allowed |= {int(n) for words in _recorded_text(facts) for n in re.findall(r"\d+", words)}
    return {int(n) for n in re.findall(r"\d+", stripped)} - allowed


def verify_stored_report(proposal_id: str, *, db_path) -> list[str]:
    """PM-30 after the fact: take a weekly report that was proposed (and perhaps approved) and prove it again from what is stored now.

    It reloads the three snapshots the proposal names from storage, recomputes every figure from them and compares it with the figures the report
    held, checks that every number in the report's text is one of those figures (or sits inside a recorded title), and that the quantities part of the report
    can be regenerated from the snapshots line for line. Returns the problems; an empty list means the report is exactly what its snapshots say today."""
    from spine.approval.proposals import ProposalNotFoundError, ProposalStore

    from pm.approval.proposals import WEEKLY_REPORT_PROPOSAL_TYPE
    from pm.reporting.weekly import compute_weekly_facts, render_weekly_report
    from pm.state.store import SnapshotNotFoundError, read_snapshot

    try:
        proposal = ProposalStore(db_path).get(proposal_id)
    except ProposalNotFoundError:
        return [f"there is no proposal {proposal_id!r}"]
    if proposal.type != WEEKLY_REPORT_PROPOSAL_TYPE:
        return [f"{proposal_id} is a {proposal.type} proposal, not a weekly report"]
    named = proposal.payload.get("snapshots") or {}
    loaded: dict[str, ProjectSnapshot] = {}
    problems: list[str] = []
    for role in ("end", "start", "previous"):
        try:
            loaded[role] = read_snapshot(named[role], db_path)
        except (KeyError, SnapshotNotFoundError):
            problems.append(f"the {role} snapshot ({named.get(role)!r}) is not stored, so the report cannot be recomputed")
    if problems:
        return problems

    end, start, previous = loaded["end"], loaded["start"], loaded["previous"]
    facts = compute_weekly_facts(end, start, previous)
    text = proposal.payload.get("content", "")
    held = {f["key"]: f["value"] for f in proposal.payload.get("figures", [])}
    fresh = recompute_figures(end, start, previous)
    for key in sorted(set(fresh) | set(held)):
        if key not in held:
            problems.append(f"{key}: recomputes to {fresh[key]} but the report did not state it")
        elif key not in fresh:
            problems.append(f"{key}: the report stated {held[key]} but the snapshots do not produce it")
        elif held[key] != fresh[key]:
            problems.append(f"{key}: the report stated {held[key]} but the snapshots give {fresh[key]}")
    problems += [f"the text states {n}, which is not one of the report's figures" for n in sorted(unexplained_numbers(facts, text))]
    stored_lines = set(text.splitlines())
    problems += [f"the report no longer matches its snapshots: the line {line!r} is not in it"
                 for line in render_weekly_report(facts).splitlines() if line.strip() and line not in stored_lines]
    return problems
