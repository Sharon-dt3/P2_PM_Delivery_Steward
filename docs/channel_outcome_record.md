# Consuming P1's channel outcome record (PM-26)

P1 (the Teams channel agent) writes one JSON file per channel per day, `outcomes/<channel>/<date>.json`, and publishes the schema
(`P3_Agents/schema/outcome_record.v1.schema.json`). This agent reads that file and turns it into two batched proposal sets for a
person to decide. The two agents were built independently and share nothing but the file and its schema: this side imports no part
of P1 to read it.

```
P1 daily job ──writes──▶ outcomes/<channel>/<date>.json ◀──published schema──▶ contracts/outcome_record.v1.schema.json (a copy)
                                      │
                       pm.channel.record  (stdlib + jsonschema only)
                                      │  refuses: unreadable, not JSON, not the schema, an unknown major version,
                                      │           a consent flag that is not exactly true
                       pm.channel.batches (plain rules, no model)
                          ┌───────────┴────────────┐
              channel_tracker_changes      channel_risk_entries        two pending proposals, each item with its channel + message
```

## Run it

```bash
uv run python scripts/consume_outcome.py --latest --dry-run            # P1's newest record, show the plan, create nothing
uv run python scripts/consume_outcome.py PATH_TO_RECORD.json            # propose both batches
uv run python scripts/consume_outcome.py --channel-id 19:...@thread.tacv2 --date 2026-10-07
```

`P1_OUTCOMES_DIR` points at P1's records if they are not in `../P3_Agents/outcomes`.

## What it proposes

| From the record | Tracker batch | Risk-log batch |
|---|---|---|
| a line naming a tracker item that exists (PM-014) | a comment on that item, tagged `from-channel` | blockers only: a risk entry, unless the item already has an open one |
| a blocker naming no item | a new item (status blocked, nobody assigned, the sprint that covers the day) | a risk entry with no item |
| a line naming an item the tracker does not have | skipped, with the reason (never guessed at) | skipped, with the reason |
| an update, decision or question naming no item | skipped: names no tracker item | n/a |

P1 splits a long message into several lines; the lines of one message in one section are one item, since the reference is the message.
Owners are never guessed (the suggested owner is the tracker's assignee, or nobody), and no severity is invented (the lead sets it).

Every item carries `reference`: the channel (id and name), the day, the message id, the section, P1's verbatim quote, and
`ref = teams:<channel_id>:<message_id>`, which the proposal also lists in its `source_refs`.

## Safe to run twice

Each item has a fingerprint (set, kind, channel, message, tracker item, and the line's words). Reading the same record again returns
the same pending proposal. A record regenerated later in the day proposes only the lines that are new. An item a person rejected is
not proposed again. A line whose wording changed is proposed again and says which earlier proposal and wording it replaces.

## What it refuses

A refused record gives zero proposals and one row in the audit log (`record.refused`, with the code and the reason):
`unreadable`, `not_json`, `schema_invalid`, `unsupported_version` (it reads 1.x; fields added in a later 1.x are tolerated), and
`not_allowlisted`: the scope/consent flag is anything other than the JSON value `true`, so P1 was not cleared to pass the channel's
content on and nothing is taken from it. This is PM-27's rule, applied at the door.

## What it does not do

Consuming a record writes nothing to the tracker or the risk log: both batches are proposals. Approving the **risk-log batch** writes its
entries to the risk log (`pm.approval.risk_apply`: the next RISK id, each entry carrying the channel message that justifies it, severity chosen
by the approver or `medium` recorded as a default, a blocker that already has an open risk skipped and the skip recorded). Approving the
**tracker batch** is refused: applying tracker changes is not built, so it can only be rejected.

## How the claim "no shared code" is checked

`tests/unit/test_channel_cross_agent.py`: P1's own code writes a record; P2 consumes the file in a process where `p1` cannot be
imported, plans both batches and creates the proposals; and the copy of the schema here must equal the one P1 publishes.
`tests/unit/test_channel_record.py` runs the reader alone with `p1` blocked.

## Scope and consent: golden case 8 (PM-27)

P1 carries a scope flag (`allowlisted`) into every record. It is what stops a channel that was never cleared from leaking into the
tracker, the risk log or the commitments. **Only the JSON value `true` passes.** Missing, `false`, `null`, `"true"`, `"yes"`, `1`,
`[true]`: all are refused as "not cleared", and the flag is looked at before anything else in the file. A record that is also not the
schema, or of an unknown major version, is refused too, under its own code. A refused record produces zero proposals, adds nothing to
the tracker, the risk log or the commitments, and leaves one row in the audit log (`record.refused`: the code, the reason, the file
and which consumer refused it) per consumer per read.

Both things in this agent that read a record go through the same reader (`pm.channel.record`): the proposal batches above, and the
commitment feed (PM-24, `scripts/commitments.py ingest`, `ingest-recent`). Before PM-27 the feed read records through P1's own
pydantic model, which turns `"true"`, `"yes"` and `1` into `True`: four of the eleven hostile records below added a commitment
anyway, and six more crashed the feed with an uncaught validation error instead of being refused.

`src/pm/eval/pm27_cases.py` writes one cleared record (twice: plain, and as a later 1.x version with fields this reader does not
know) and eleven kinds of record that must not be used, every one carrying the same content (a commitment and a blocker, each
naming a real tracker item, so a leak would show). Each is read twice by both consumers, each against its own database.

| Metric | Must be | What it catches |
|---|---|---|
| `GC8-leaked-proposal-count` | 0 | a proposal made from a record that was refused |
| `GC8-leaked-write-count` | 0 | a row a refused record added to the tracker, risk log or commitments |
| `GC8-accepted-refusable-record-count` | 0 | a consumer that took a record it should have refused |
| `GC8-refusal-log-mismatch-count` | 0 | a refusal row missing, extra, or under the wrong reason |
| `GC8-wrongly-refused-count` | 0 | a cleared record refused (the control) |
| `GC8-refused-record-count` | at least 11 | the eleven kinds really were refused, each for the labelled reason |
| `GC8-cleared-control-output-count` | at least 6 | the cleared control really produced both batches and its commitment |
