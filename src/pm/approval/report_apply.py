"""Applying an approved weekly status report: it is SAVED as the final version, never sent.

The plan says the agent never sends the weekly report: a person does. So approving it means "this is the version I will send". It is written, exactly
as proposed, to one file per week ending, in PM_REPORTS_DIR (default data/reports/weekly/weekly_report_<week ending>.md), through the same write gate as
every other write (guarded_send refuses anything not approved), and the audit says where, and says it was not sent. The report cannot be edited
here: a figure changed by hand would no longer recompute (PM-30), so it is approved as proposed or rejected.

Idempotent: saving the same text again changes nothing. A file for that week that holds DIFFERENT text is never overwritten: the approval is refused
before it is recorded, so nothing is left approved-but-not-saved.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from spine.approval.proposals import Proposal

from pm.approval.proposals import WEEKLY_REPORT_PROPOSAL_TYPE

REPORT_WRITE_TYPES = frozenset({WEEKLY_REPORT_PROPOSAL_TYPE})
ENV_DIR = "PM_REPORTS_DIR"
_REPO_ROOT = Path(__file__).resolve().parents[3]
_WEEK_ENDING = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ReportApplyRefused(Exception):
    """The approved report cannot be saved, with the reason in words."""


@dataclass(frozen=True)
class ReportSave:
    path: Path
    content: str
    already_saved: bool  # the file is there with exactly this text


def reports_dir() -> Path:
    return Path(os.environ.get(ENV_DIR) or (_REPO_ROOT / "data" / "reports")) / "weekly"


def plan_save(proposal: Proposal) -> ReportSave:
    """Where the report would be saved and whether that is possible. Raises ReportApplyRefused when it is not."""
    payload = proposal.payload
    week_ending = str(payload.get("local_date", ""))
    content = payload.get("content", "")
    if proposal.type not in REPORT_WRITE_TYPES:
        raise ReportApplyRefused(f"a {proposal.type} proposal is not a weekly report")
    if not _WEEK_ENDING.match(week_ending):
        raise ReportApplyRefused(f"the report has no valid week ending ({week_ending!r}), so there is no file name for it")
    if not content.strip():
        raise ReportApplyRefused("the report is empty: there is nothing to save")
    path = reports_dir() / f"weekly_report_{week_ending}.md"
    text = content.rstrip("\n") + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") == text:
            return ReportSave(path, text, already_saved=True)
        raise ReportApplyRefused(f"{path.name} already exists with different text: it is never overwritten")
    return ReportSave(path, text, already_saved=False)


def save(plan: ReportSave) -> Path:
    """Write the report. Atomic: the file is either absent or complete."""
    if plan.already_saved:
        return plan.path
    plan.path.parent.mkdir(parents=True, exist_ok=True)
    temporary = plan.path.with_suffix(".md.partial")
    temporary.write_text(plan.content, encoding="utf-8")
    temporary.replace(plan.path)
    return plan.path
