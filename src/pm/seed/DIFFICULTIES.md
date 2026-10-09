# PM-03: the ten planted difficulties

The P2 golden set. Each one below is a real, queryable property of a row in
`src/pm/seed/build.py` -- never a label that only a comment asserts. Each is
independently re-derived (a date comparison, a missing foreign key, a status
outside the enum) by its own test in `tests/unit/test_seed_difficulties.py`,
the same bar a real PM-04 adapter or detector would have to clear.

Anchor date for every "as of" comparison below: **2026-09-18** (`ANCHOR_DATE`
in `build.py`).

## 1. A stale blocker with no risk entry, and a fresh blocker for contrast

> "Blocker open 4 days with no risk entry, and another open 1 day."

- **PM-014** ("Search index blocked on staging DB migration") -- `status=blocked`,
  `blocked_since=2026-09-14` (4 days before the anchor). No row in `risks`
  has `related_item_id = "PM-014"`.
- **PM-015** ("Export job null-case handling blocked pending product decision")
  -- `status=blocked`, `blocked_since=2026-09-17` (1 day before the anchor).
  Also has no matching `risks` row, but here that absence is expected --
  it just opened.
- Proven by: `test_difficulty_1_stale_blocker_has_no_matching_risk_entry`,
  `test_difficulty_1_fresh_blocker_is_the_contrast_case`.

## 2. An item moved to done and back to in-progress the same day

> "An item moved to done then back to in-progress the same day."

- **PM-016** ("Reporting dashboard export bug") -- `item_transitions` rows:
  `in_progress -> done` at `2026-09-16T10:00:00`, then
  `done -> in_progress` at `2026-09-16T15:30:00`. Same calendar day, current
  status `in_progress`.
- Proven by: `test_difficulty_2_item_flapped_done_and_back_same_day`.

## 3. An assignee with zero activity for two days

> "An assignee with zero activity for two days."

- **aisha.rahman** -- no `item_transitions`, `item_comments`, or `commits`
  row of hers falls on 2026-09-16 or 2026-09-17 (the two full days before
  the anchor), despite her last touching her assigned item **PM-017** with a
  comment on 2026-09-15, and having earlier activity on other items before
  that.
- Unique on purpose: every other roster member with any history (noah.becker,
  wei.chen, mateo.silva, olivia.dupont, olivia.dupree) has activity on 2026-09-16
  or 2026-09-17. The only other member with nothing on those days is
  **sofia.lindqvist**, who has no activity at all -- PM-10's separate planted
  case, not one of these ten -- so a "no activity on 09-16/17" detector returns
  exactly {aisha.rahman, sofia.lindqvist}, and aisha.rahman is the only one of
  the two who had any activity before.
- Proven by: `test_difficulty_3_assignee_has_a_two_day_activity_gap_before_anchor`,
  `test_difficulty_3_aisha_is_the_only_member_with_prior_activity_who_goes_quiet`.

## 4. An unassigned item

> "An unassigned item."

- **PM-018** ("Caching layer PR review") -- `assignee_id = NULL`. Still
  reaches `status=in_review` via `item_transitions` despite having no
  assignee -- being unassigned doesn't stop work happening.
- Proven by: `test_difficulty_4_exactly_one_item_is_unassigned`.

## 5. Two items added mid-sprint

> "Two items added mid-sprint."

- **PM-019** (`created_at=2026-09-12`) and **PM-020** (`created_at=2026-09-15`)
  -- both in `sprint-13` (`start_date=2026-09-07`), both created more than 3
  days after the sprint started, unlike every other Sprint 13 item.
- Unique across the whole seed, not just Sprint 13: every other item, in either
  sprint, was created within 2 days of its own sprint's start (Sprint 12's
  items all sit between 2026-08-24 and 2026-08-26).
- Proven by: `test_difficulty_5_two_items_were_added_mid_sprint`,
  `test_difficulty_5_no_other_item_in_any_sprint_was_added_mid_sprint`.

## 6. A commit with no item reference

> "Commits with no item reference..."

- **Commit `b8888bb`** ("chore: bump CI runner image to node 20") --
  `item_ref = NULL`. The only such commit among all 43 seeded.
- Proven by: `test_difficulty_6_one_commit_has_no_item_reference`.

## 7. An item referenced by a commit but never transitioned

> "...and one item referenced but never transitioned."

- **PM-021** ("Retry queue backoff jitter") -- referenced by commit
  `b9999cc` ("add jitter to retry backoff calculation"), but has zero rows
  in `item_transitions`; still sits at its creation status, `backlog`.
- Proven by: `test_difficulty_7_item_referenced_by_a_commit_was_never_transitioned`.

## 8. A free-text status outside the tracker's enum

> "A free-text status that does not map to the enum."

- **PM-022** ("Billing sync vendor API migration") -- `status="waiting_on_vendor"`,
  which is not a member of `CANONICAL_STATUSES`
  (`backlog`/`in_progress`/`blocked`/`in_review`/`done`). The schema itself
  places no `CHECK` on `items.status`, so this is a real value a consumer
  can read, not a rejected one.
- Proven by: `test_difficulty_8_one_item_has_a_free_text_status`.

## 9. A commitment with only a relative due date

> "A commitment with a relative due date only."

- **Commitment**: `member_id=mateo.silva`, `item_id=PM-016`,
  `due_date_iso=NULL`, `due_date_text="end of week"`. The only commitment
  among the 8 seeded with no ISO date, contrasted against the other 7,
  which all carry a real `due_date_iso`.
- Proven by: `test_difficulty_9_one_commitment_has_only_a_relative_due_date`.

## 10. Two assignees with similar names

> "Two people with similar names."

- **olivia.dupont** ("Olivia Dupont") and **olivia.dupree** ("Olivia Dupree")
  -- same first name, last names sharing the 3-letter prefix "Dup" before
  diverging. Reused verbatim from P1's own "Project Gamma" channel roster,
  not invented for this repo (see `build.py`'s own module docstring).
- Proven by: `test_difficulty_10_two_assignees_have_similar_names`.

---

Source of truth for all ten: `src/pm/seed/build.py`. Source of proof for all
ten: `tests/unit/test_seed_difficulties.py`. This file exists so the set can
be named and reviewed on its own, without reading either.


## PM-31: how difficulty 8 (the free-text status) is handled

A tracker's own statuses are an exact list (`CANONICAL_STATUSES`). Anything else, however close it looks, is **UNMAPPED** and carries the value the tracker holds, never
corrected into the nearest status (`In Progress`, `in-progress` and `done ` are all UNMAPPED). It is shown, with its raw value, in:

- the morning brief: `- Unmapped: PM-022 (Billing sync vendor API migration): UNMAPPED, the tracker says 'waiting_on_vendor', which is not one of its statuses.`
  under the item's owner, worded by code (a model could tidy the raw value into a nearby status, so it never sees the item), and not counted under Pending;
- the end-of-day diff: `PM-017 moved from in_progress to UNMAPPED ('waiting_on_legal').` A move between two different unmapped values is a change, not
  "UNMAPPED to UNMAPPED". The footer counts unmapped items without naming them and does not count them as open;
- the weekly report: `PM-022 has a status the tracker does not map: UNMAPPED (the tracker says 'waiting_on_vendor').`
- proven by `tests/unit/test_unmapped_status.py`.


## PM-32: how difficulty 10 (the similar names) is handled

`olivia.dupont` and `olivia.dupree` are two people with two sections in every brief. Resolution is by one rule (`src/pm/people.py`, written up in `docs/people_resolution.md`): a person is
their id; only an exact id, alias or display name resolves to them. A bare `Olivia`, `olivia d` or `Dup` could be either, so it is credited to neither and listed as ambiguous; `O. Dupont`
or a typo resembles one but is not merged into them. Proven by `tests/unit/test_people_resolution.py`.
