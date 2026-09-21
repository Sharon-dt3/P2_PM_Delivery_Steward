"""PM-03: the ten difficulties planted in PM-01/02's seed data. Each
test re-derives its difficulty from the actual rows (a date comparison,
a missing foreign key, a status outside the enum) rather than trusting
build.py's own comments -- the same bar a real PM-04 adapter/detector
would have to clear.
"""

from __future__ import annotations

from datetime import date, timedelta

from pm.seed.build import ANCHOR_DATE, ASSIGNEES, CANONICAL_STATUSES, ITEMS


def _item(item_id: str) -> dict:
    return next(item for item in ITEMS if item["id"] == item_id)


# 1. A stale blocker (no matching risk log entry) alongside a fresh
#    blocker (no risk entry expected yet) for contrast.
def test_difficulty_1_stale_blocker_has_no_matching_risk_entry(seeded_conn):
    stale = _item("PM-014")
    assert stale["status"] == "blocked"
    blocked_since = date.fromisoformat(stale["blocked_since"])
    assert (ANCHOR_DATE - blocked_since).days >= 4

    row = seeded_conn.execute(
        "SELECT COUNT(*) AS n FROM risks WHERE related_item_id = ?", (stale["id"],)
    ).fetchone()
    assert row["n"] == 0


def test_difficulty_1_fresh_blocker_is_the_contrast_case(seeded_conn):
    fresh = _item("PM-015")
    assert fresh["status"] == "blocked"
    blocked_since = date.fromisoformat(fresh["blocked_since"])
    assert (ANCHOR_DATE - blocked_since).days <= 1

    row = seeded_conn.execute(
        "SELECT COUNT(*) AS n FROM risks WHERE related_item_id = ?", (fresh["id"],)
    ).fetchone()
    assert row["n"] == 0  # also missing -- but expected, since it just opened


# 2. An item moved to done and back to in_progress the same calendar day.
def test_difficulty_2_item_flapped_done_and_back_same_day(seeded_conn):
    rows = seeded_conn.execute(
        "SELECT from_status, to_status, changed_at FROM item_transitions WHERE item_id = ? ORDER BY id",
        ("PM-016",),
    ).fetchall()
    done_rows = [row for row in rows if row["to_status"] == "done"]
    reopened_rows = [row for row in rows if row["from_status"] == "done" and row["to_status"] == "in_progress"]

    assert len(done_rows) == 1
    assert len(reopened_rows) == 1
    assert done_rows[0]["changed_at"][:10] == reopened_rows[0]["changed_at"][:10]
    assert done_rows[0]["changed_at"] < reopened_rows[0]["changed_at"]
    assert _item("PM-016")["status"] == "in_progress"


# 3. An assignee with zero transitions/comments/commits for two full
#    days immediately before ANCHOR_DATE, despite recent prior activity.
def test_difficulty_3_assignee_has_a_two_day_activity_gap_before_anchor(seeded_conn):
    member_id = "aisha.rahman"
    gap_start = ANCHOR_DATE - timedelta(days=2)
    gap_end = ANCHOR_DATE - timedelta(days=1)
    gap_days = {gap_start.isoformat(), gap_end.isoformat()}

    transition_days = {
        row["changed_at"][:10]
        for row in seeded_conn.execute(
            """
            SELECT it.changed_at FROM item_transitions it
            JOIN items i ON i.id = it.item_id
            WHERE i.assignee_id = ?
            """,
            (member_id,),
        ).fetchall()
    }
    comment_days = {
        row["created_at"][:10]
        for row in seeded_conn.execute(
            "SELECT created_at FROM item_comments WHERE author_id = ?", (member_id,)
        ).fetchall()
    }
    commit_days = {
        row["committed_at"][:10]
        for row in seeded_conn.execute(
            "SELECT committed_at FROM commits WHERE author_id = ?", (member_id,)
        ).fetchall()
    }
    activity_days = transition_days | comment_days | commit_days

    assert not (activity_days & gap_days)
    assert any(day < gap_start.isoformat() for day in activity_days)


# 4. An unassigned item.
def test_difficulty_4_exactly_one_item_is_unassigned():
    unassigned = [item for item in ITEMS if item.get("assignee_id") is None]
    assert len(unassigned) == 1
    assert unassigned[0]["id"] == "PM-018"


# 5. Two items added mid-sprint (created well after their sprint's own
#    start_date, unlike everything else in that sprint).
def test_difficulty_5_two_items_were_added_mid_sprint(seeded_conn):
    sprint = seeded_conn.execute(
        "SELECT start_date FROM sprints WHERE id = ?", ("sprint-13",)
    ).fetchone()
    threshold = date.fromisoformat(sprint["start_date"]) + timedelta(days=3)

    mid_sprint_adds = [
        item
        for item in ITEMS
        if item["sprint_id"] == "sprint-13" and date.fromisoformat(item["created_at"]) > threshold
    ]
    assert len(mid_sprint_adds) == 2
    assert {item["id"] for item in mid_sprint_adds} == {"PM-019", "PM-020"}


# 6. A commit with no item reference.
def test_difficulty_6_one_commit_has_no_item_reference(seeded_conn):
    row = seeded_conn.execute("SELECT COUNT(*) AS n FROM commits WHERE item_ref IS NULL").fetchone()
    assert row["n"] == 1

    orphan = seeded_conn.execute("SELECT sha, message FROM commits WHERE item_ref IS NULL").fetchone()
    assert orphan["sha"] == "b8888bb"


# 7. An item referenced by a commit but never transitioned out of its
#    created status (the code-host adapter's own paired edge case).
def test_difficulty_7_item_referenced_by_a_commit_was_never_transitioned(seeded_conn):
    referencing_commits = seeded_conn.execute(
        "SELECT COUNT(*) AS n FROM commits WHERE item_ref = ?", ("PM-021",)
    ).fetchone()
    assert referencing_commits["n"] >= 1

    transitions = seeded_conn.execute(
        "SELECT COUNT(*) AS n FROM item_transitions WHERE item_id = ?", ("PM-021",)
    ).fetchone()
    assert transitions["n"] == 0
    assert _item("PM-021")["status"] == "backlog"


# 8. A free-text status value outside the tracker's canonical enum.
def test_difficulty_8_one_item_has_a_free_text_status():
    outliers = [item for item in ITEMS if item["status"] not in CANONICAL_STATUSES]
    assert len(outliers) == 1
    assert outliers[0]["id"] == "PM-022"
    assert outliers[0]["status"] == "waiting_on_vendor"


# 9. A commitment with only a relative due-date phrase, alongside others
#    that carry a proper ISO date for contrast.
def test_difficulty_9_one_commitment_has_only_a_relative_due_date(seeded_conn):
    relative_only = seeded_conn.execute(
        "SELECT * FROM commitments WHERE due_date_iso IS NULL"
    ).fetchall()
    assert len(relative_only) == 1
    assert relative_only[0]["due_date_text"] == "end of week"

    dated = seeded_conn.execute("SELECT * FROM commitments WHERE due_date_iso IS NOT NULL").fetchall()
    assert len(dated) >= 1
    for row in dated:
        date.fromisoformat(row["due_date_iso"])  # raises ValueError if malformed


# 10. Two assignees with similar-enough names that a naive text match
#     could confuse them.
def test_difficulty_10_two_assignees_have_similar_names():
    display_names = {assignee["id"]: assignee["display_name"] for assignee in ASSIGNEES}
    dupont = display_names["olivia.dupont"].split()
    dupree = display_names["olivia.dupree"].split()

    assert dupont[0] == dupree[0]  # same first name
    assert dupont[1] != dupree[1]  # different, but...

    shared_prefix_len = 0
    for a, b in zip(dupont[1], dupree[1]):
        if a != b:
            break
        shared_prefix_len += 1
    assert shared_prefix_len >= 3  # "Dup" of Dupont/Dupree
