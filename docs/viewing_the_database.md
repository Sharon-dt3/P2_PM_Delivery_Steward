# Seeing what is in P2's database

P2 stores everything in one SQLite file, `data/pm.db`. There are two ways to look at
it. Both are read-only and neither changes what the app does.

## 1. Locally, in your browser (nothing leaves the machine)

```bash
uv run --with sqlite-web python scripts/view_db.py
```

Open http://127.0.0.1:8081 . Every table is listed, with search and sorting. It reads
the live file, so reload the page to see new rows as jobs and approvals run. It is
read-only (a write attempt fails with "attempt to write a readonly database") and is
served on 127.0.0.1 only, so it is not reachable from the network. Stop it with Ctrl-C.
`sqlite-web` is fetched on demand by uv; it is not a project dependency.

## 2. Supabase mirror (so someone else can look without being on your Mac)

A one-way copy of the SQLite file into Postgres. SQLite stays the source of truth and
nothing reads the copy back. It goes into its own schema, `p2`, so it can never mix with
P1's mirror, which lives in the default schema.

1. Put `SUPABASE_DB_URL` in `.env` (Supabase: Project Settings, Database, the Direct
   connection URI, password percent-encoded). It is a credential and is never printed
   or logged.
2. Copy once: `uv run python scripts/sync_to_supabase.py` (add `--dry-run` to list the
   tables first).
3. In Supabase's Table Editor, choose the `p2` schema.
4. To keep it fresh automatically, set `PM_SUPABASE_MIRROR=1`: the scheduled jobs and
   every approve, reject or send then refresh it in the background. It returns at once,
   bursts are coalesced, and a failure is only logged: it can never affect the app.

Two safeguards keep a wrong database from ever reaching the mirror: the automatic
refresh only ever syncs the configured database (`PM_DB_PATH`, default `data/pm.db`,
compared as real paths), and the test suite pins the mirror off. (A test run once
pushed a temporary database over the real mirror because the dashboard tests load the
real `.env`; the mirror was restored, and both safeguards now have tests.)
`scripts/sync_to_supabase.py --db FILE` can still mirror any file, deliberately and by name.

The copy is only as fresh as its last sync, and every column is text (it is for
reading, not for querying). Rows deleted in SQLite are deleted in the mirror.
Tested against a real Postgres: 158 rows in 13 tables, identical on a second run.

## Which writes change the file

| Action | What changes |
|---|---|
| morning job | a snapshot, a proposal, an audit row |
| end-of-day job | a snapshot |
| approve / reject / auto-approve / retry | the proposal, audit rows, a write-log row if it sends |
| `scripts/seed.py` | the sample project tables |
