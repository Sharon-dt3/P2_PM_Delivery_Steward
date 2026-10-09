# Edge cases and failures (PM-35)

Seven situations a real day produces and a demo day does not. Each is run through the real pipeline (a database, stored snapshots, the facts, the model, the
grounding, the rendering) by `src/pm/eval/pm35_cases.py`, and every claim in the resulting reports is compared with the stored snapshots alone. The seven are
probes inside GC2, so a fabrication in any of them counts toward the one headline number, which stays a hard zero.

**The check.** `unsupported_morning_progress` and `unsupported_eod_progress` read the *rendered text*, not the facts it was made from, so they catch a wrong fact as
well as a wrong sentence. They check, against the snapshots: that every item named exists; that a "delivered" item is done, a "pending" item open, a "blocked"
item blocked; that it is credited to the person the tracker says owns it; that an item listed as unassigned has no assignee and is not done; the sprint count; that
each commit listed as tied to no work really ties to none; and, for the end-of-day summary, that "shipped" is done now and was not done this morning, "newly
blocked" was not blocked this morning, "still pending" moved and ended open, each "moved from A to B" is what the two snapshots show, and the unchanged count.

| Scenario | What the pipeline does | Where it is tested |
|---|---|---|
| Empty day | A project with nothing in it: reports name no item, the summary never calls the model. A full project on a quiet day: "none" in every section, no model call, no move reported. | `scenario_empty_day`; `test_a_project_with_nothing_in_it_*`, `test_a_quiet_day_*` |
| Person with no activity | Shown as "No update", never given to the model, absent from the end-of-day summary. | `scenario_person_with_no_activity`; `test_a_person_with_no_activity_*` |
| Item that moved twice | One line, with its path. Blocked then done is shipped once. Done, reopened and done again is *not* shipped today; neither is done then reopened; an item blocked all day that flapped is *not* newly blocked. | `scenario_item_moved_twice`; `test_an_item_blocked_then_done_*`, `test_done_then_reopened_*`, `test_an_item_blocked_all_day_*` |
| Malformed model output | Eight shapes (prose, `[]`, `null`, wrong types, missing fields, no lines, invented references, cut off). The section falls back to the recorded facts; nothing is lost and nothing invented. | `scenario_malformed_model_output`; `test_malformed_model_output_loses_no_fact_and_invents_none` (8 cases) |
| Rate-limit path | The model unreachable for the whole run, or from the third call on. The reports are made from the recorded facts, marked as recorded; the model is asked **once** for the whole run; the job says which sections read as recorded. | `scenario_rate_limit_path`; `test_with_the_model_unreachable_*`, `test_the_real_gateway_giving_up_*`, the job tests |
| Unassigned item | Listed under "Nobody is assigned", credited to nobody, shown as `Owner: unassigned` when it ships; a finished one is not listed as work nobody owns. | `scenario_unassigned_item`; `test_an_unassigned_finished_item_*` |
| Commit with no item reference | Listed under "Commits with no item reference" with its words; no item changes status; a commit that names an item the tracker does not have does not make the report name it. | `scenario_commit_with_no_item_reference`; `test_a_commit_that_names_no_item_*` |

## What the pass found and fixed

The scenarios found no fabrication. They found that a model failure was not survivable in three of the five places a model is asked:

| Where the model is asked | Before | After |
|---|---|---|
| Morning brief | A rate limit that outlasted the retries, or three malformed answers, raised out of the generator: no brief that day. | Each section that cannot be written is shown as the recorded facts, marked; the job result names those sections. |
| Weekly narrative | The same: the weekly report job crashed, though the narrative is optional. | The report is its quantities and the recorded facts, with no model prose and no closing sentence. |
| End-of-day summary | Already fell back per section, but asked a dead gateway once per section. | Asks it once. |
| Risk-log proposals | Already fell back per blocker (to template text), but asked a dead gateway once per blocker. | Asks it once. |
| Weekly closing sentence | A malformed answer, or the gateway failing, raised out of the narrative. | The report has no closing sentence; the lines stay. |

A dead gateway (rate limits spent, local fallback down) is tried **once per run**, because each try costs a full round of backoff.

## How we know the checks fire

A clean scenario proves little unless the check can fail. `tests/unit/test_edge_cases_pm35.py` changes one claim in an otherwise clean report for each rule above and
requires the check to find it. Separately, each safeguard and each rule was removed in turn and the tests were required to fail: 17 mutations of the product
(a done-again item counted as shipped, a blocked-again item counted as newly blocked, the unassigned list inverted, commits with no reference left out, and each of the
model-failure paths) and 14 of the checks themselves. Three survived at first (the EOD tests had changes in only one section, so "asked once" was true without the
breaker; no test moved an item into the "pending" or "blocked" section); tests were added and they were killed.

## What this does not cover

- The scenarios use a scripted model and hand-built bad outputs. Real Claude was run through GC1/GC2 only (see `docs/eval_findings.md`); it was not made to rate-limit.
- The channel brief and the nudges do not call a model in P2, so they are not in this pass.
- "Nothing fabricated" is measured against the snapshots. A snapshot that is itself wrong (a tracker that is out of date) is outside what P2 can detect.
