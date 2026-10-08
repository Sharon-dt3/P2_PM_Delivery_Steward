# The weekly status report (PM-29, PM-30)

A client-facing **draft**, made from three stored project snapshots taken seven days apart (`end`, `start` a week before, `previous` a week before that).
The agent **never sends it**: it is offered as a proposal that can be read and rejected, and nothing carries it out.

    uv run python scripts/weekly_report.py --db data/pm.db --at 2026-09-18T17:00            # make it, store the snapshots, propose it
    uv run python scripts/weekly_report.py --db data/pm.db --at 2026-09-18T17:00 --dry-run  # show and check it; store and propose nothing

## What it says, and where each part comes from

| Section | Computed from |
|---|---|
| Progress against sprint scope | the current sprint's items in the `end` snapshot: done of total, by status; what finished this week (done at `end`, not done at `start`) |
| Scope change | items created **more than `PM_SCOPE_GRACE_DAYS` (default 2) days after the sprint started** ("added after planning"), and the items that joined the sprint this week |
| Top risks | open risks in the `end` snapshot, high first, then the oldest; at most five |
| Decisions needed from the client | blocked items, oldest first, with how long each has been blocked. What to decide is not guessed |
| Velocity | completed this week against the week before (the `start` and `previous` snapshots), as a percentage, or no percentage when the week before completed nothing. It lists what changed in the same week and says it does not say why |
| Statuses | a status the tracker uses that is not in the enum is shown as `UNMAPPED` with its raw value; it is counted nowhere else |

No model writes any of it: a fixed template over the facts, so the same snapshots make the same report.

## PM-30: every number recomputes

`pm.reporting.weekly_check.recompute_figures` re-derives every figure from the stored snapshots with its own plain code, and `check_report` compares:
each figure against what the report states, and every number in the report's text (once ids, dates, the sprint's name and rank markers are set aside)
against the figures, so a number that was never computed cannot be in the text. The job reads the snapshots **back from storage** before computing,
and a report that does not recompute is never offered as a proposal: the problems are printed instead.

## The proposal

Type `weekly_status_report`, one per week ending (idempotent). Its card in Teams has **Reject only**; approving it is refused ("nothing carries it out").
