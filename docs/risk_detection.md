# Risk detection (PM-16)

For each **current blocker that has no risk-log entry**, the agent creates one proposal. Nothing is
written to the risk log: a proposal is something a person reads and can reject.

## What is code and what is the model

| | Who | Where |
|---|---|---|
| Which items are blockers (tracker status `blocked` at the snapshot's moment) | Python | `pm/risk/gaps.py` |
| Which are covered (any risk, in any status, naming the item) | Python | `covered_items` |
| Days blocked, days left in the sprint, days a commitment is overdue | Python | `BlockerGap` |
| Suggested owner = the tracker's assignee, or nobody | Python | `BlockerGap.owner` |
| The **description** and **impact** wording | the model (Ollama gemma2:9b today, Claude later) | `pm/risk/proposals.py` |

The model is shown only the evidence for the one blocker (`reference_id: item:PM-014`, never an
already-logged one). Each line it writes must carry that reference and a verbatim quote from the
evidence, use only words/ids/numbers the evidence contains, state only durations Python computed and
dates the evidence states. A line that fails is retried, then dropped, and that half of the proposal
is filled by a fixed template built from the same facts: a real gap is never lost to a model failure.
Each proposal records `prose_source` (`model` or `template`) per field, and keeps what grounding
dropped in `original_model_output`.

## A proposal contains

`description`, `impact`, `suggested_owner` (`{id, name, evidence}` or `null`: shown as "no owner is
evidenced"), `blocker_ref` (`item:PM-014`), `evidence` (the facts, with commit/message/commitment
references), and `facts` (the computed numbers).

## Running it

```bash
uv run python scripts/detect_risks.py --dry-run            # the gaps only, no model, nothing created
uv run python scripts/detect_risks.py --gateway scripted   # no model: prose straight from the facts
uv run python scripts/detect_risks.py                      # the configured model (local Ollama)
uv run python scripts/approve.py list                      # then read them; reject any that is wrong
```

The morning job does the same after the brief when `PM_RISK_DETECTION=1` (default off). A failure
there never stops the brief. Re-running never repeats a proposal (key: item + blocked-since date; a
rejected one is not re-proposed, a blocker that clears and is blocked again is a new blockage).

## Approval

Risk proposals show on the dashboard (Reject only) and in `approve.py list`. Approving one is
refused up front, and auto-approve never takes one, because applying an approved entry to the risk
log is not built (it needs a severity decision). The refusal is enforced in the service, not just
hidden in the UI.

## Rejection memory (PM-17)

A rejected proposal is **fingerprinted**: a hash of the blocker's *material* facts (when it became
blocked, the assignee, the title, the sprint, and the commitments on it with their due dates),
stored on the proposal (`fingerprint`, `material_facts`) and kept when it is rejected. The next run
compares the blocker as it is now with every earlier proposal for that item, in code, before any
model is asked:

| Memory says | Run does |
|---|---|
| nothing earlier | proposes |
| an open proposal with the same facts | leaves it (already proposed) |
| a **rejected** proposal with the same facts | does not propose again |
| an earlier proposal with other facts is still open | does not pile a second one on it |
| every earlier one rejected, and the facts differ | proposes again, **stating the change** |

Not material, so they can never bring a rejected proposal back: the passing of time (the day count),
new commits or chat on the blocker, and the model's wording.

When it proposes again, the new proposal carries `change`: the rejected proposal it follows, who
rejected it and why, and each difference (`blocked since was 2026-09-14, now 2026-09-18`, `assignee was
Olivia Dupree, now Wei Chen`, `commitment 6 due date was ..., now ...`). The statement is written by code,
shown above the evidence, and the audit entry links the two (`follows_rejected`). Going back to facts that were
already rejected is not proposed again. Proposals rejected before fingerprints existed (PM-16) are
compared on the blocked-since date and the owner they recorded, so nothing already rejected is forgotten.

`detect_risks.py --dry-run` shows, per gap, what the memory would do.

## Acceptance (PM-16)

On the seeded project the gap set is **PM-014** and **PM-015**; PM-023 (RISK-001) and PM-024
(RISK-002) are already logged and RISK-003 names no item, so none of those three is proposed
(`tests/unit/test_risk_proposals.py::test_the_two_missing_blockers_are_proposed_and_the_ones_already_logged_are_not`).

**PM-17:** reject one proposal, rerun, and no duplicate appears
(`tests/unit/test_risk_rejection_memory.py::test_reject_one_rerun_and_no_duplicate_appears`).
