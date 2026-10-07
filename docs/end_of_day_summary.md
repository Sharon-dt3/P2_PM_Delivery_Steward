# End-of-day summary (PM-22)

At the project's end-of-day time the agent proposes a summary of **what genuinely changed since the morning**:
what shipped, what is still pending, what is newly blocked, and what else changed. It is built from the
**stored diff**, never from the morning brief: Python computes the delta and the sections, the model only
words each line.

## Where it comes from

1. The end-of-day job (`pm/jobs/end_of_day_job.py`) takes and stores the end-of-day snapshot.
2. It finds that day's **stored morning snapshot** (the earliest one stored earlier the same local day). If none
   was stored, the morning state is rebuilt from the tracker's history, nothing is saved, and the summary says so.
3. `pm.state.diff.compute_delta` diffs the two: one entry per item that genuinely changed, however many times it
   moved (an item that went to done and back is one "churned" entry, not two).
4. `pm/reporting/end_of_day_facts.py` places each changed item in **exactly one** section, by what happened to it
   (first match wins):

| Section | An item is in it when |
|---|---|
| What shipped | it ended `done` and was not done this morning |
| What is newly blocked | it ended `blocked` and was not blocked this morning |
| What is still pending | its status moved and it ended open (backlog, in_progress, in_review) |
| What else changed since morning | anything else that changed: churn that ended where it began, a new open item, an item gone from the tracker, a handover with no status move |

So an item that moved twice (blocked, then done) is **one line under What shipped**, with its path
("moved from in_progress to blocked to done"), never also under newly blocked.

5. For each section with something in it, the model is shown only that section's facts and asked for one line per
   fact (prompt `pm22_end_of_day_summary`, versioned). Every line must cite its own fact, carry a verbatim quote, and
   use only words, ids and numbers the fact contains: the same checks the brief uses. A line that fails is retried,
   then dropped, and the change is shown as the recorded fact, marked `[as recorded]`. Nothing is asked of the model
   when nothing changed.
6. A line the model wrote by title alone gets its item id put in front (once), so every line says which item it is
   about and each item is named exactly once.
7. Open items that did **not** change are not named; they are only counted ("14 other open items did not change today.").
   That is the morning brief's job, and the summary is never a restatement of it.

## What happens next

The job only **proposes** the summary (one per channel per local day). It goes through the same approval gate as the
brief: a person approves, edits or rejects it (dashboard, `scripts/approve.py`, a Teams card), or, with
`PM_AUTO_APPROVE=1`, the system approves it when every line grounded and a person has approved something for the
channel before. Nothing is posted until then.

## Trying it

```bash
uv run python scripts/run_scheduler.py --gateway scripted --once --job end-of-day --at 2026-09-16T17:00
uv run python scripts/approve.py list
```

(Use `--gateway llm` for the real model. Add `--db PATH` to work on a throwaway copy.)

## Acceptance (PM-22)

The summary names exactly the hand-labelled changed set (golden case 3: PM-016, PM-018, PM-020) and the item that moved
twice (PM-016) appears once: `tests/unit/test_eod_summary.py::test_the_summary_names_exactly_the_hand_labelled_changed_set_and_the_twice_moved_item_appears_once`.
