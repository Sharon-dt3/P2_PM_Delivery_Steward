# What the real-model run found (PM-33, PM-34)

The scripted gateway cannot show how the checks behave against a language model's own wording, so PM-33 ran the same nine golden cases on Claude
(`bedrock:global.anthropic.claude-sonnet-4-6`). The first live run failed GC2, a hard zero. Every number below is a record in `eval/results.jsonl`.

| Step | Code revision | Live Claude, GC2 fabricated claims | Metrics passing |
|---|---|---|---|
| First live run | `6585762` | **2** | 40 of 41 |
| The probe works out a blocker's label from the facts | `a3583dc` | **1** | 40 of 41 |
| The probe accepts another form of a word the fact uses | `96eb993` | **0** | 41 of 41 |

The scripted run passed 41 of 41 before and after, at every step.

## What was wrong

Neither failure was Claude inventing something. Both were the independent probe (`count_fabrications`, written separately from the production grounding check on purpose)
disagreeing with the production code about wording the production code rightly allows.

1. **The blocker label.** The brief puts `[severity] RISK-id:` in front of a blocker line itself when the model leaves it out, because those come from the risk log. The probe compared
   the rendered line with the model's text exactly, so every blocker line a model wrote without the label counted as a fabrication. The scripted gateway writes the label itself, so it never showed.
   The probe now works the expected label out from the facts, for the risk the line cites. A line labelled with another severity or another risk's id is still a fabrication
   (`tests/unit/test_gc2_blocker_labels.py`).
2. **An inflection.** One run, Claude wrote "saying" where the fact says "says". The production check compares words by stem and allowed it; the probe compared the first four letters and
   did not. The probe now also accepts a word whose root (without one ending: ing, ed, es, s) is a word of the fact. A word the fact does not have in any form is still flagged.

## What this does not claim

The probe was made more accurate, not more lenient about claims: the first change added a check (a wrong label is flagged, which it never was), the second accepts only another form of a word
the fact already contains. Model output varies from run to run: a live GC2 of 0 is one run, not a guarantee, and the second change was made because a differently worded run produced
"saying". Re-run `uv run python scripts/run_eval.py --live-bedrock` to add a record; the README shows the newest.
