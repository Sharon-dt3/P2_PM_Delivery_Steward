# P2 -- PM Delivery Steward

Second agent in the Incubation Pod Three-Agent Delivery Plan (P2 of
P1/P2/P3). See `../P3_Agents/docs/MASTER_IMPLEMENTATION_PLAN.md` for the
full plan; this repo starts at **D11**, the plan's scoped starting point
for P2:

- **PM-01/PM-02** -- seed data: 25-40 tracker items across 2 sprints,
  5-7 assignees, commit history, a risk log, delivery commitments, and 2
  P1 `OutcomeRecord`s (one with `allowlisted=False`, standing in for "one
  missing its scope/consent flag"). Channel messages are **reused from
  P1's own fixture** (`../P3_Agents/seed/fixtures`), not re-seeded here.
- **PM-03** -- ten deliberately planted difficulties in that seed data
  (see `src/pm/seed/DIFFICULTIES.md` for the exact list and which entity
  each one lives on).
- **PM-04** -- three new adapters (tracker, code host, risk-log store),
  each a narrow interface with a mock over the seeded fixture, plus
  confirmation that P1's Teams reader and publisher satisfy this agent's
  chat dependency with **zero new adapter code** -- see
  `src/pm/adapters/` once that task lands for exactly how they're wired.

## Why this is a separate repo

Per the user's explicit choice: P2 lives in its own sibling folder next
to `P3_Agents` on disk, not inside it. It depends on two path
dependencies onto the sibling repo -- `p1` (the whole P1 project, for
its adapters and config/storage conventions) and `spine` (the shared
internal package P1 extracted at Gate G1 / CHN-33) -- declared in
`pyproject.toml` under `[tool.uv.sources]`. Both repos are expected to
stay checked out as siblings under the same parent folder (e.g.
`~/Desktop/P3_Agents` and `~/Desktop/P2_PM_Delivery_Steward`) for the
relative paths to resolve.

## Status

D11 is fully built:

- **Scaffold**: `pyproject.toml`, the `src/pm`/`tests/unit` layout, and
  a smoke test confirming the cross-repo path dependencies on `p1` and
  `spine` actually import.
- **Seed data** (`src/pm/seed/build.py`): 30 tracker items across 2
  sprints, 6 assignees, 43 commits, a 3-entry risk log, 8 delivery
  commitments (some overdue), and 2 P1 `OutcomeRecord`s (one
  `allowlisted=False`), plus the ten planted difficulties from PM-03,
  each a real, queryable property of the data -- named with the entity
  each lives on in `src/pm/seed/DIFFICULTIES.md`, and independently
  re-derived from the rows (not trusted from a comment) by
  `tests/unit/test_seed_difficulties.py`. Run `uv run python
  scripts/seed.py` to build `data/pm.db` and the outcome-record
  fixtures from scratch.
- **Adapters** (`src/pm/adapters/`): `TrackerMock`, `CodeHostMock` and
  `RiskLogMock` -- narrow interfaces (`Tracker`/`CodeHost`/
  `RiskLogStore`), each with a mock over the seeded fixture -- plus
  `teams.py`, which wires P1's own `MockTeamsReader`/`LogPublisher`
  (imported unchanged, not reimplemented) at this repo's own paths, so
  the chat dependency is satisfied with zero new adapter code. See each
  module's own docstring for which PM-04 edge case it covers and where.

## Getting started

```
uv sync
uv run python scripts/seed.py
uv run pytest tests/unit -q
```
