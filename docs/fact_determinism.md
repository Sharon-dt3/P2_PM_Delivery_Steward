# Determinism of the brief's facts (PM-23, golden case 9)

The morning brief's **wording** may differ from one generation to the next. What a reader takes from it may not: the
**set of items, who owns them, the counts, and each item's status**.

GC9 generates the brief twice from the same snapshot (the second time from the stored copy of it, the way the job reads it
back), through the whole pipeline (facts, then the model, then grounding), with the two generations worded differently. It
then reads a **fact set** out of each rendered brief, from the text a person reads, with a parser that shares no code with
the generator, and compares the two:

| Fact | What it is |
|---|---|
| sprint | its id, "day N of M", "X of Y items done" |
| owner | every person with a section (and "no update" for a person with nothing) |
| item | (owner, status bucket, item id): who has delivered / pending / blocked what |
| count | how many items in each bucket, per owner |
| due | a commitment's due date, per owner (read as a date wherever the line puts it) |
| blocker | its rank and risk id, and its severity |

Zero divergences is the acceptance. Four numbers are recorded in `eval/results.jsonl`:

- `GC9-fact-divergence-count` (hard zero): facts that differ between the two generations
- `GC9-snapshot-fact-gap-count` (hard zero): each generation also matches the facts computed from the snapshot, so two briefs cannot "agree" by both dropping the same thing
- `GC9-snapshot-reload-change-count` (hard zero): the stored copy of the snapshot is the same snapshot
- `GC9-wording-difference-count` (at least 1): the two generations really were worded differently, so zero divergence is not two identical briefs

The standard run (`scripts/run_eval.py`) uses two scripted stand-ins that word every line differently. To check a real model:

```bash
uv run python scripts/check_fact_determinism.py            # the scripted pair, instant
uv run python scripts/check_fact_determinism.py --gateway llm   # the configured model (Claude on Bedrock), twice; real calls
```

## What running it on a real model found

The first live run on Claude Sonnet 4.6 showed the Blockers section reduced to bare sentences ("Billing sync nightly job is
at risk of missing SLA.") with no risk id and no severity: a reader could not tell which risk it was or how severe. The
ranking was right (code sorts it) but the identity was lost to the wording. The brief renderer now puts the risk id and the
severity in front of any blocker line that lacks them, once (`pm.reporting.morning_brief._identified`), and leaves a line
that already carries them alone. The same live run also showed the parser had to read dates and sprint numbers out of natural
phrasing rather than the scripted phrasing; it does.
