"""PM-01/02's own DoD: the seed's shape matches what the master plan's
D11 scope asks for. See src/pm/seed/build.py's own docstring for the
data itself; test_seed_difficulties.py covers PM-03's ten planted
difficulties specifically."""

from __future__ import annotations

from datetime import date

from pm.seed.build import ANCHOR_DATE, ASSIGNEES, COMMITMENTS, COMMITS, ITEM_TRANSITIONS, ITEMS, RISKS, SPRINTS


def test_item_count_is_within_the_planned_25_to_40_range():
    assert 25 <= len(ITEMS) <= 40


def test_item_ids_are_unique():
    ids = [item["id"] for item in ITEMS]
    assert len(ids) == len(set(ids))


def test_exactly_two_sprints():
    assert len(SPRINTS) == 2


def test_assignee_count_is_within_the_planned_5_to_7_range():
    assert 5 <= len(ASSIGNEES) <= 7


def test_every_item_belongs_to_a_real_sprint():
    sprint_ids = {sprint["id"] for sprint in SPRINTS}
    assert {item["sprint_id"] for item in ITEMS} <= sprint_ids


def test_every_assigned_item_has_a_real_assignee():
    assignee_ids = {assignee["id"] for assignee in ASSIGNEES}
    for item in ITEMS:
        assignee_id = item.get("assignee_id")
        if assignee_id is not None:
            assert assignee_id in assignee_ids


def test_no_item_transitions_before_its_own_created_at():
    """A basic internal-consistency guard: an item can't have moved
    status before it existed."""
    transitions_by_item: dict[str, list[dict]] = {}
    for row in ITEM_TRANSITIONS:
        transitions_by_item.setdefault(row["item_id"], []).append(row)

    for item in ITEMS:
        rows = transitions_by_item.get(item["id"], [])
        if not rows:
            continue
        first_change_day = rows[0]["changed_at"][:10]
        assert first_change_day >= item["created_at"]


def test_seed_lands_in_the_database_unchanged(seeded_conn):
    (item_count,) = seeded_conn.execute("SELECT COUNT(*) FROM items").fetchone()
    assert item_count == len(ITEMS)
    (sprint_count,) = seeded_conn.execute("SELECT COUNT(*) FROM sprints").fetchone()
    assert sprint_count == len(SPRINTS)
    (assignee_count,) = seeded_conn.execute("SELECT COUNT(*) FROM assignees").fetchone()
    assert assignee_count == len(ASSIGNEES)


def test_every_seeded_store_actually_lands_in_the_database(seeded_conn):
    """PM-02's own acceptance test, literally: "All stores seeded and
    committed." Every table build_seed() writes to must actually hold
    the rows the Python-side lists claim, not just have the right count
    in COMMITS/COMMITMENTS/RISKS themselves -- this is the one test
    that checks all of them against the real, migrated schema in one
    place, including the two (commits, commitments) no adapter-level
    test already covers end to end."""
    table_and_expected = {
        "sprints": len(SPRINTS),
        "assignees": len(ASSIGNEES),
        "items": len(ITEMS),
        "item_transitions": len(ITEM_TRANSITIONS),
        "commits": len(COMMITS),
        "commitments": len(COMMITMENTS),
        "risks": len(RISKS),
    }
    for table, expected in table_and_expected.items():
        (actual,) = seeded_conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        assert actual == expected, f"{table}: expected {expected} seeded rows, found {actual}"


def test_build_seed_is_idempotent(seeded_conn):
    """PM-01/02's own DoD line: "seed committed and reproducible."
    Calling build_seed() again on an already-seeded connection must
    land on the exact same row counts, not accumulate duplicates."""
    from pm.seed.build import build_seed

    build_seed(seeded_conn)
    (item_count,) = seeded_conn.execute("SELECT COUNT(*) FROM items").fetchone()
    assert item_count == len(ITEMS)


def test_risk_log_has_three_entries_two_matching_current_blockers(seeded_conn):
    """PM-04's own risk-log adapter edge case: "three pre-existing
    entries, two matching current blockers.\""""
    risks = seeded_conn.execute("SELECT * FROM risks").fetchall()
    assert len(risks) == 3
    assert len(RISKS) == 3

    blocker_ids = {item["id"] for item in ITEMS if item["status"] == "blocked"}
    matching = [risk for risk in risks if risk["related_item_id"] in blocker_ids]
    non_matching = [risk for risk in risks if risk["related_item_id"] not in blocker_ids]
    assert len(matching) == 2
    assert len(non_matching) == 1


def test_commit_count_is_within_the_planned_30_to_60_range():
    """PM-02's own DoD line: "30-60 commits referencing some but not
    all items.\""""
    assert 30 <= len(COMMITS) <= 60


def test_commits_reference_some_but_not_all_items():
    referenced_item_ids = {c["item_ref"] for c in COMMITS if c["item_ref"] is not None}
    all_item_ids = {item["id"] for item in ITEMS}
    assert referenced_item_ids  # some
    assert referenced_item_ids < all_item_ids  # not all -- a strict subset


def test_commitment_count_is_within_the_planned_6_to_10_range():
    """PM-02's own DoD line: "six to ten commitments, some overdue.\""""
    assert 6 <= len(COMMITMENTS) <= 10


def test_some_commitments_are_overdue_as_of_the_anchor_date():
    """"Overdue" here means a real due_date_iso that has already passed
    ANCHOR_DATE while nothing in this seed shows the commitment kept --
    not merely a due date sitting in the future."""
    overdue = [
        c for c in COMMITMENTS
        if c["due_date_iso"] is not None and date.fromisoformat(c["due_date_iso"]) < ANCHOR_DATE
    ]
    assert len(overdue) >= 2
