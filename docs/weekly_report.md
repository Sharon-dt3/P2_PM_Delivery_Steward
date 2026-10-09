# The weekly status report (PM-29, PM-30)

A client-facing **draft**, made from three stored project snapshots taken seven days apart (`end`, `start` a week before, `previous` a week before that).
The agent **never sends it**. It is offered as a proposal; **approving it saves it as the final version** (see "Approving"), and a person sends it.

**Python computes every quantity; the Claude API writes the narrative** (the plan's split for PM-29, the same one as P1's weekly roll-up: "rates and trends
are computed, never estimated by a model; the narrative is the only generated part"). The week window and the trend (this week minus the week before, by the
same arithmetic) follow P1's roll-up.

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

Every section and figure is a fixed template over the facts, so the same snapshots make the same figures. The model writes only two things, both under
**Velocity**, and both are held to the facts (`pm.reporting.weekly_narrative`, prompts `pm29_weekly_narrative` and `pm29_weekly_closing`):

- **"In plain language"**: one line per fact (the velocity change, each item added after planning, each blocked item, each unmapped status), in plainer
  words. Each line cites its fact and quotes it verbatim, and is grounded exactly like the morning brief's lines: reference-or-drop, a verbatim quote,
  and no id, number or word the fact does not have. A line that fails is dropped, logged, and shown as the recorded fact (`[as recorded]`).
- **"In short"**: one closing sentence on how the week went against the one before. P1's rule applies and is enforced in code, not requested: **no digit, no
  number word, no id, no claim of cause**, only words the facts contain plus a few neutral comparison words (more, less, some), and each claim must match a
  fact ("more work" only when the velocity fact says up; "blocked" only when something is blocked). A refused sentence is asked again with the reason, up to
  three times; if the model never complies there is no closing sentence.

With no facts to rephrase, the model is not called. `--gateway scripted` writes the same section without a model (the facts in plainer order), and
`--gateway none` leaves the narrative out. What the model wrote and what grounding dropped are kept in the proposal.

## PM-30: every number recomputes

`pm.reporting.weekly_check.recompute_figures` re-derives every figure from the stored snapshots with its own plain code, and `check_report` compares:
each figure against what the report states, and every number in the report's text, **narrative included** (once ids, dates, the sprint's name and rank
markers are set aside), against the figures, so a number the model brought in cannot be in the text. The job reads the snapshots **back from storage** before computing,
and a report that does not recompute is never offered as a proposal: the problems are printed instead.

## The proposal

Type `weekly_status_report`, one per week ending (idempotent). Its card in Teams has **Approve** and **Reject**.

## Approving

The plan says the agent never sends the report, so approving it means **"this is the version I will send"**:

- **Approve** saves the text, exactly as proposed, to `data/reports/weekly/weekly_report_<week ending>.md` (`PM_REPORTS_DIR` moves the folder), through the
  same write gate as every other write (`guarded_send` refuses anything not approved). **Nothing is sent anywhere**; the audit says so
  (`proposal.applied` with `target: report_file`, the path and `sent: false`), and so does the decision card.
- **No edit box**: a figure changed by hand would no longer recompute from the snapshots (PM-30), so the report is approved as proposed or rejected.
- **Refused before it is recorded** (the proposal stays pending) when the file for that week already holds *different* text (never overwritten), or the
  report has no valid week ending or no text. Saving the same text again changes nothing. A failed save leaves it approved, and a retry saves it once.
- **Auto-approve never takes it.** Only a person approves a report.
- GC6 covers it like every other write: `GC6-report-write-bypass-count` (22 attempts against pending and rejected reports) and
  `GC6-report-audit-gap-count` (approved and rejected, including that the saved file is exactly the approved text and that nothing was sent).

## On a schedule

Opt-in: `PM_WEEKLY_REPORT=1` in `.env` (or `--weekly-report` on the scheduler). The scheduler then makes the report **Fridays at 18:00 in Asia/Colombo**, 15 minutes after
P1's Friday weekly digest. `PM_WEEKLY_REPORT_AT="Fri 18:00"` and `PM_WEEKLY_REPORT_TZ` change the day, time and timezone. `--weekly-gateway llm|scripted|none`
says who writes the narrative (default: the model set by the environment). It only creates the proposal; the card in Teams has Reject only.

A model that is down costs the report its narrative, not its existence (logged as `weekly_report_narrative_unavailable`). A run that cannot make the report at
all logs why (`weekly_report_failed`) and never takes the scheduler down. One report per week ending, so a second run the same week proposes nothing new.

    uv run python scripts/run_scheduler.py --once --job weekly --at 2026-09-18T18:00 --db /tmp/scratch.db --weekly-gateway scripted

What the report says depends on the project it reads: on the seeded sample project (Sprint 13 ended on 20 September) a report for a later date says "No sprint on file
covers this date". The risks section reads the shared risk log, so it shows real risks.

## Keeping a current sprint

The report measures the sprint that covers its date, and new items from a channel go to the sprint that covers their day (or, when none does, to the sprint named in
`PM_TRACKER_DEFAULT_SPRINT`). Nothing picks a sprint for you. When a sprint ends, add the next one, or the report says "No sprint on file covers this date" and new
items fall back to the named default:

    uv run python scripts/add_sprint.py --id sprint-15 --name "Sprint 15" --start 2026-10-19 --end 2026-11-01 --by sharon --dry-run   # check it, change nothing
    uv run python scripts/add_sprint.py --id sprint-15 --name "Sprint 15" --start 2026-10-19 --end 2026-11-01 --by sharon

`--move PM-031,PM-032` also moves items into it. A range may not overlap another sprint; every change is in the audit log (`sprint.added`, `item.moved_sprint`).
Sprint 14 (5 to 18 October) was added this way.
