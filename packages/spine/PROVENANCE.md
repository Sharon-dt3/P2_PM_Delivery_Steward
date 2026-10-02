# Provenance

This is P2's own copy of the shared `spine` package, exported with
`git archive` from the P1 repo (`Sharon-dt3/P3_Agents`) at commit
`6238200` ("Fix CI lint failures and two test gaps the full suite caught"),
path `packages/spine`, unmodified. Its own `uv.lock` was dropped (a workspace
member uses the root lock).

P2 may change this copy freely; P1's spine is not touched. At PM-40 ("spine
hardening for P3") the two are compared with `diff -r` and the changes worth
keeping are folded back into P1's spine deliberately, not implicitly.
