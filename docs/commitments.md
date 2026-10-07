# Commitment tracking, nudges and escalation (PM-24)

A commitment is a promise to do something by a day. This agent tracks them from the day they are made to the day they are
kept, reminds the person before the due date, records when one slips, and tells the lead when one is overdue beyond a
threshold. The arithmetic and the rules are Python; the words are fixed templates, so what a person is told about their
own commitment is exactly what was recorded. No model is involved.

## The store: seeded, and fed by channel outcome records

- The seeded `commitments` table is the starting point (source `seed`). Migration 0005 gives each commitment a status
  (`open`, `fulfilled`, `cancelled`), a close date and a source, and adds `commitment_events` and this agent's side of the
  shared `nudges` ledger.
- **Fed by P1's outcome records** (`pm/commitments/outcomes.py`): for each update or decision line in a record, rules decide
  whether it is a promise (not a status report: "Pushed the fix, should resolve the flaky test" is not one), resolve its due
  date against the day it was said ("tomorrow", "end of week", "by Friday", an ISO date), and find who said it from the
  message the line cites (an outcome record names no author). A record with `allowlisted: false` is not read at all (P1 was
  not cleared to pass the day on). A promise from someone not on the project is reported, not added. Reading a record again
  adds nothing.
- A promise with no day in it is stored as "no date given" and never held to a date it did not give.
- A commitment is open until it is closed, or until its item reaches `done` (the tracker is the truth about the work).

## The follow-up pass (`pm/commitments/followup.py`)

On each working day, for every open commitment:

| Situation | What happens |
|---|---|
| due within the next N days | one **reminder** to the person, as a direct message through P1's publish adapter |
| past its due date | one **overdue record** (nothing is sent: a record that it slipped, and when) |
| overdue by **more than** T days | one **escalation** to the lead, with the evidence (what was promised, when, how late, when the person was reminded, the item's status, the source message) |

Guards:

- **The person hears first.** The lead is told only after the person was reminded on an earlier day. An overdue commitment
  never reminded gets its reminder first, and the lead hears a day later.
- **First-time approval.** The first reminder this agent ever sends to a person, and the first escalation about a person,
  wait for a person to approve them (`scripts/approve.py list`). After that they go out unattended, recorded as an
  automatic approval under the system's own id.
- **The shared cap** (below).
- Nobody on the project's exceptions list (on leave) is reminded or escalated about. Nothing is sent on a non-working day
  (an overdue record is still written). A commitment that is done or closed is never chased, and a reminder waiting for
  approval is refused if the commitment is closed before it is approved. Each follow-up happens once per commitment (a
  database constraint).
- Reminders and escalations are proposals through the same approval gate as the brief (`pm/approval/service.py`), as
  direct messages.

N, T and the cap come from configuration, never a literal in the code: P1's channel config supplies the lead
(`channel_owner_id`), the escalation threshold (`escalation_threshold_days`), the cap (`nudge_cap_per_day`) and who is on
leave; environment variables override them for this agent (see `.env.example`).

## The ageing view

`uv run python scripts/commitments.py ageing` lists every open commitment, most overdue first, grouped (overdue more than 7
days, 3 to 7, 1 to 2, due today, due in 1 to 3 days, due later, no due date), with who, how long it has been open, how
overdue, the item's status, and what has been done about it.

## Running it

```bash
uv run python scripts/commitments.py ageing
uv run python scripts/commitments.py run --dry-run            # what would happen; nothing changes, nothing is sent
uv run python scripts/commitments.py run                      # proposes, and sends what is allowed to go unattended
uv run python scripts/commitments.py ingest-recent --days 7   # read P1's recent outcome records
uv run python scripts/commitments.py close 6                  # fulfilled by hand (add --cancel to cancel)
```

The morning job runs the pass (outcome records, then follow-ups) after the brief when `PM_COMMITMENT_FOLLOWUP=1`. Off by
default. A failure there never stops the brief.
