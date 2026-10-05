# Provenance

This is P2's own copy of the shared `spine` package, exported with
`git archive` from the P1 repo (`Sharon-dt3/P3_Agents`) at commit
`6238200` ("Fix CI lint failures and two test gaps the full suite caught"),
path `packages/spine`, unmodified. Its own `uv.lock` was dropped (a workspace
member uses the root lock).

P2 may change this copy freely; P1's spine is not touched. At PM-40 ("spine
hardening for P3") the two are compared with `diff -r` and the changes worth
keeping are folded back into P1's spine deliberately, not implicitly.

## Changes made in this copy since the export

- `spine.grounding.kernel`: an optional `content_check` hook on `verify_line`,
  `verify_lines` and `ground_with_retry` (failure reason `content_not_supported`).
  Backwards compatible: with no hook the kernel behaves exactly as before.
  P2 uses it to require a verbatim quote and to reject ids/numbers a brief line
  invents. Four new tests in `tests/test_grounding.py`. Candidate for folding
  back into P1's spine at PM-40 ("citation resolver generalisation").
- `spine.eval.runner.run_eval`: an optional `extra` dict of run-level fields
  (P2 records the git revision and a dirty flag) merged into the run record
  without overriding any existing field. Backwards compatible. Covered by P2's
  `tests/unit/test_eval_pm12_strict.py`. Candidate for folding back at PM-40.
