# The risk log

The delivery risk log lives in three places that are kept in sync.

| Copy | Where | Role |
|---|---|---|
| **Repo CSV** | `risk_log/risks.csv` (this folder, committed) | The **system of record**. Open it in Excel, Numbers or any editor. Fresh databases and offline runs start from exactly this. |
| **Lead-facing table** | Supabase, schema `p2`, table `risk_log` | What the **delivery lead opens and edits**. It stands in for the Dataverse table the plan names, which this account cannot create. It has real columns and constraints (severity must be low, medium or high; status open, mitigated or closed). |
| **Runtime copy** | the `risks` table in `data/pm.db` | What the morning brief reads. Always loaded from the repo CSV; never edit it by hand. |

## The columns

`id` (RISK-001), `title`, `description`, `severity` (low | medium | high), `status`
(open | mitigated | closed), `related_item_id` (a tracker item such as PM-023, or empty),
`opened_at` (a date like 2026-09-10), and optionally `owner` (who has it, as "Name (id)", e.g.
`Olivia Dupree (olivia.dupree)`).

`owner` is only ever set where it is evidenced: when a person approves a risk proposal, the owner
the proposal suggested (the tracker's assignee of the item) is written onto the entry, and the audit
keeps the evidence. An entry with no evidenced owner has none, and a log with no owners has no
`owner` column at all, so the file looks exactly as it always did until the first one appears. The
lead can type or change an owner in the table or the CSV like any other field.

Only **open** risks appear as blockers in the brief, so closing or mitigating one removes it
from tomorrow's brief.

## Keeping them in sync

```bash
uv run python scripts/risk_log.py show             # the log as a table
uv run python scripts/risk_log.py status           # compare the three copies; exit 1 if they differ
uv run python scripts/risk_log.py sync --dry-run   # show what a sync would do, change nothing
uv run python scripts/risk_log.py sync             # three-way sync
```

`sync` compares both sides with the last state they agreed on:

* only the lead edited the table: the edit flows into the repo CSV and the runtime copy;
* only the repo CSV changed: it flows to the lead's table;
* **both** changed: a conflict. It names what differs and changes nothing; resolve it with
  `push` (the repo wins) or `pull` (the lead wins);
* the lead's table suddenly empty is not taken as an instruction to wipe the log.

A bad edit is refused with every problem named and nothing is applied: an unknown severity,
an empty title, a date that is not a date, a risk pointing at a tracker item that does not exist.
If the lead's table cannot be reached, nothing changes and the repo copy keeps working offline.

Set `PM_RISK_LOG_SYNC=1` and each morning job first pulls the lead's edits, so the brief
reflects them. After editing the CSV by hand, run `sync` (or `push`) and commit the file.

**Approving a risk proposal** (in Teams, the dashboard or `approve.py`) writes the entry to the CSV
and the runtime copy, and, with `PM_RISK_LOG_SYNC=1`, pushes it to the lead's table straight away
(the same sync, so only the repo moved). The approval says which happened: "the lead's table is up
to date", or "NOT up to date yet (remote_unreachable | conflict | ...)". The write stands either way,
in the CSV; if the table could not be updated, nothing of the lead's is overwritten and the next
sync (the morning job's) brings it level. A refused approval never touches the lead's table.

The first time the sync touches a lead's table made before `owner` existed, it adds the column
(`ADD COLUMN IF NOT EXISTS`: additive, nothing dropped or rewritten).

## Settings

`SUPABASE_DB_URL` (a credential, never printed), `PM_SUPABASE_SCHEMA` (default `p2`),
`PM_RISK_LOG_SYNC` (default off). See `.env.example`.
