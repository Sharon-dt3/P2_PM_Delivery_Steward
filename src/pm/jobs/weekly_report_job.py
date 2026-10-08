"""Make the weekly status report (PM-29/PM-30): store three project snapshots a week apart, compute the report from the STORED copies, check it recomputes,
and offer it as a proposal. It is never sent: nothing carries this proposal type out, so a person can read it and reject it, and that is all.

Reading the snapshots back from storage before computing (not using the objects just built) is the point: the figures are exactly what a later reader of
the stored snapshots would get, which is what PM-30 asserts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pm.approval.proposals import propose_weekly_report
from pm.reporting.weekly import WeeklyFacts, compute_weekly_facts, render_weekly_report
from pm.reporting.weekly_check import check_report
from pm.reporting.weekly_narrative import Narrative, generate_narrative
from pm.state.snapshot import ProjectSnapshot, build_current_snapshot
from pm.state.store import DuplicateSnapshotError, read_snapshot, save_snapshot
from pm.storage.db import DEFAULT_DB_PATH

WEEK = timedelta(days=7)


@dataclass(frozen=True)
class WeeklyReport:
    facts: WeeklyFacts
    narrative: Narrative
    text: str
    problems: list[str]  # what did not recompute; empty when every figure does
    proposal_id: str | None = None
    created: bool = False


def _stored(moment: datetime, db_path, timezone_name: str) -> ProjectSnapshot:
    taken_at = moment.astimezone(timezone.utc).isoformat()
    try:
        save_snapshot(build_current_snapshot(db_path, taken_at=taken_at, tz_name=timezone_name), db_path)
    except DuplicateSnapshotError:
        pass  # this moment was already stored: the report uses what is stored
    return read_snapshot(taken_at, db_path)


def run_weekly_report_job(
    moment: datetime | None = None, *, db_path: str | Path = DEFAULT_DB_PATH, timezone_name: str = "Asia/Colombo", propose: bool = True,
    gateway=None,
) -> WeeklyReport:
    """`gateway` is the model that writes the narrative; with None there is no narrative and the report is the quantities alone."""
    now = moment or datetime.now(timezone.utc)
    end, start, previous = (_stored(now - n * WEEK, db_path, timezone_name) for n in (0, 1, 2))
    facts = compute_weekly_facts(end, start, previous)
    narrative = generate_narrative(facts, gateway) if gateway is not None else Narrative()
    text = render_weekly_report(facts, narrative if gateway is not None else None)
    problems = check_report(facts, text, end, start, previous)  # the narrative's text is checked too: a number the model brought in is not a figure
    if not propose or problems:  # a report that does not recompute is never offered: say what is wrong instead
        return WeeklyReport(facts, narrative, text, problems)
    proposal, created = propose_weekly_report(
        text=text, figures=[f.model_dump() for f in facts.figures],
        snapshots={"end": end.taken_at, "start": start.taken_at, "previous": previous.taken_at}, week_ending=facts.week_ending, db_path=db_path,
        narrative={"lines": [{"text": line.text, "reference_id": line.message_id, "quote": line.quote} for line in narrative.lines],
                   "dropped": narrative.dropped, "closing": narrative.closing, "closing_refusals": narrative.closing_refusals} if gateway is not None else None,
    )
    return WeeklyReport(facts, narrative, text, problems, proposal.id, created)
