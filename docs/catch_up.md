# Catch-up on startup

APScheduler keeps its schedule in memory. A fire time that passes while the scheduler is stopped is not late, it is forgotten (the 6-hour misfire grace only
covers a process that was running but delayed). So a scheduler stopped on Friday afternoon and started on Saturday used to skip Friday without saying so.

`scripts/run_scheduler.py` now makes up what was missed when it starts. Mechanism: `packages/spine/src/spine/scheduling/catchup.py`. P2's decisions:
`src/pm/scheduling/catch_up.py`. Tests: `tests/unit/test_catch_up.py`.

## What it does

1. Every scheduled run that finishes is recorded in a `scheduler_runs` table in the scheduler's own database (`--db`), under the moment it was *scheduled for*.
2. At startup, for each cron job (the sample project's morning brief and end-of-day summary, each real channel's morning and evening brief, the weekly report), it finds
   the job's **latest** fire time inside the lookback window. If no finished run is recorded for it, the job is run once, now, with `moment=` that scheduled time, so it
   does Friday's work and not Saturday's.
3. The makeup runs go to the scheduler's own threads, so a slow model never holds up startup. The banner says what is being made up.

## Rules, and why

| Rule | Why |
|---|---|
| **The first start only arms the ledger.** Only fire times after it was armed are ever made up. | The first start after installing cannot know whether this morning's brief already ran; re-running it would duplicate work. It also means **a stop that happened before the first catch-up start cannot be made up automatically**. |
| **Only the latest missed run of each job.** | A morning brief for Thursday, made on Saturday, is noise. |
| **Within `PM_CATCH_UP_HOURS` (default 72; `0` turns it off).** | A stop on Friday afternoon is made up on Monday morning; anything older is history. |
| **More than 6 hours late: proposed for a person, never auto-approved,** whatever `PM_AUTO_APPROVE` says. | An end-of-day summary posted on its own the next day is news about a day that is over. |
| **A run is recorded only if it did its work.** A crash, a morning brief that was not generated, an end-of-day summary whose model failed, an evening channel brief before P1 wrote its record, a weekly report that could not be made: each is tried again at the next start. | Otherwise one bad run would be remembered as a good one. |
| **A job is made up only if it can be told which moment to work for** (it takes `moment`), and a late one only if it can be held (it takes a `policy`, or it never sends, as the weekly report). | The alternative is a late run that posts itself. |

Idempotency is the second safety: every job keys its proposal by channel and local date (or week ending), so a made-up run that finds the day already proposed makes no second one.

## Switches

- `--no-catch-up` on the scheduler, or `PM_CATCH_UP_HOURS=0`: off, and nothing is written to the database.
- `PM_CATCH_UP_HOURS=N`: how far back a missed run is made up.
- `--print-schedule` starts nothing and writes nothing.

## Not covered

- **P1's runners** (`live_runner_*`) have their own copy of spine in `P3_Agents` and are not changed. P1's digests are idempotent per channel and day, so a catch-up there would be safe, but it is a change in that repo.
- Data a job reads **as of a past moment**: the tracker's history is reconstructed point-in-time, but P1's channel records are read by day. A made-up channel brief is built from the day's record, whatever the clock says.
- A scheduler that was never started under catch-up has no ledger; see the first rule.
