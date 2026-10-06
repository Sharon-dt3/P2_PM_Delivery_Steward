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
`opened_at` (a date like 2026-09-10).

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

## Settings

`SUPABASE_DB_URL` (a credential, never printed), `PM_SUPABASE_SCHEMA` (default `p2`),
`PM_RISK_LOG_SYNC` (default off). See `.env.example`.
