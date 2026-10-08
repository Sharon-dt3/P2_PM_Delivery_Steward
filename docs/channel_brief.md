# The channel brief

A morning brief and an end-of-day summary for a **real** Teams channel, made only from real Teams data. Nothing in it is generated and nothing
in it comes from the seeded sample project.

## Where every line comes from

| Source | What it gives | How it is read |
|---|---|---|
| **P1's outcome record** (`outcomes/<channel>/<date>.json`) | the day's grounded blockers, decisions, updates and open questions, each with the Teams message it came from, and who on the roster had no say | through the published contract (`pm.channel.record`): the consent flag must be exactly `true` and the schema must match, or nothing is taken |
| **P1's message store** (`P1_DB_PATH`, default `data/p1_live.db`) | who posted each message and when (the record names no author) | read-only; if it is missing no author is shown, and one is never guessed |
| **What this agent did with those messages** | tracker items (`source_message_id`) and risk entries whose text cites a message of the record | found by the real message ids; the sample project's items never match |

Every line is one of P1's grounded lines exactly as P1 verified it, with its author and a `[source](...)` link to the exact Teams message (P1's
stored permalink, the same form as P1's daily digest; with no link stored the line shows the message id, never an invented link), or a fact computed in code (a promise's due
date, an item made from a message, who had no say). The same facts always make the same message. A section with nothing in it says
"none recorded"; a long one is cut at 5 lines and says how many more there are.

## Which record

* **Morning** (08:00 in the channel's own timezone): the newest record **before today**, i.e. what the team said on the last working day. If that
  record is more than a day old the brief says how old it is.
* **Evening** (P1's daily digest time + 15 minutes): **today's** record. P1 writes it after its digest, so the evening summary waits for it and
  never reads an old one in its place.
* Nothing real (no record in the last 7 days, a record P1 was not cleared to pass on, a file that is not the contract) means **nothing is
  proposed** and the reason is said. A refused record leaves a `record.refused` row, like P2's other readers.

## Sections

Blockers, Decisions, Updates, Open questions (always shown); **Said they would** (promises, read with the existing commitment rules, "tomorrow" resolved
against the day it was *said*, each marked past due / due today / coming up and never "done", because nothing here can see whether it was);
**Turned into work** (items and risk entries made from these messages, e.g. `PM-031 (blocked)`, `RISK-004 (high)`); **No say that day**
(roster members the record lists as having posted nothing).

## The approval gate

A channel brief is a proposal like any other (the same morning-brief and end-of-day-summary types, so the same Teams card, dashboard, audit and
post): one per channel per day per kind, **nothing sent until a person approves it**, posted only to a channel on P1's allowlist. The payload
says it came from a channel record and which one, and the proposal keeps every line with the message behind it.

## Automatic end to end

Nothing in a normal day is run by hand:

1. **The scheduler** (`run_scheduler.py --channel-briefs ...`) makes each channel's morning and evening brief at its own time, and from the
   same record also proposes the **tracker** and **risk-log** batches (`PM_CHANNEL_BATCHES=0` turns that off; reading a record again proposes nothing new).
2. **A Power Automate flow on a 5-minute timer** calls the API's `ClaimNewApprovals` action (`/claim_new_approvals`). It returns only the pending
   proposals no card has been sent for, and records each as handed out in the same transaction (`pm.approval.card_delivery`), so a card is posted
   **once**, and two overlapping runs never get the same proposal. A proposal still undecided after `PM_CARD_RESEND_HOURS` (default 24; 0 = never) is
   handed out once more as a reminder. The plain `ListPendingApprovals` still answers "what is waiting".
3. **A person presses Approve** on the card in Teams; the flow calls `CardAction` with their email as the approver.
4. **Approving a brief posts it** through P1's publish flow to that channel, and only to an allowlisted one.

## Running it

```bash
# make the brief for a channel and look at it, proposing nothing
uv run python scripts/run_channel_brief.py --channel p1-agent-test --kind morning --dry-run
# propose it (a card arrives through the approvals flow)
uv run python scripts/run_channel_brief.py --channel p1-agent-test --kind morning --db data/pm.db
# schedule both real channels (the sample project is not scheduled unless --with-sample-project)
uv run python scripts/run_scheduler.py --channel-briefs p1-agent-test,teams-agent-test --db data/pm.db
```

Settings: `PM_CHANNEL_BRIEFS`, `PM_OUTCOMES_DIR`, `PM_CHANNEL_BRIEF_LINES`, `PM_CHANNEL_BRIEF_LABEL` (see `.env.example`). Posting for real needs
`TEAMS_PUBLISHER_MODE=power_automate` and `POWER_AUTOMATE_FLOW_URL` (the same publish flow P1 uses).

## What it does not do

It does not read a real tracker or code host (there is none: those are seeded mocks by design), so it says nothing about sprint scope or commits.
Its "promises" use P2's existing commitment heuristic, which misses some phrasings ("is planned by Monday"); every one is shown verbatim with its
author so a reader can judge it.
