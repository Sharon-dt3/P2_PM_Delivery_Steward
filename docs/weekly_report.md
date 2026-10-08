# The weekly status report (PM-29, PM-30)

A client-facing **draft**, made from three stored project snapshots taken seven days apart (`end`, `start` a week before, `previous` a week before that).
The agent **never sends it**: it is offered as a proposal that can be read and rejected, and nothing carries it out.

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

Type `weekly_status_report`, one per week ending (idempotent). Its card in Teams has **Reject only**; approving it is refused ("nothing carries it out").
