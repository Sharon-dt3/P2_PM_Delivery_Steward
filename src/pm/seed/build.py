"""
PM-01/02 (D11): the seed data this repo's own three new adapters (PM-04)
run their mocks over, plus PM-03's ten deliberately planted difficulties
baked directly into this same data rather than flagged separately -- a
difficulty here is a real, queryable property of a row (a status value,
a missing reference, a date gap), never a label a test merely trusts.
tests/unit/test_seed_difficulties.py re-derives each one from these rows
the same way a real adapter would, rather than asserting against this
module's own comments.

Everything below is a static, hand-authored literal -- no randomness, no
datetime.now()/date.today() anywhere in this module -- so build_seed()
and build_outcome_fixtures() produce byte-identical output on every run.
That is what "seed committed and reproducible" (D11's own DoD line)
means here.

Two things this data deliberately reuses rather than invents, per the
master plan's own note that channel messages are "reused from P1's
fixture, not re-seeded":

1. The six people below (noah.becker, aisha.rahman, wei.chen,
   olivia.dupont, mateo.silva, olivia.dupree) are P1's own "Project
   Gamma" (19:proj-gamma@thread.tacv2) channel roster, verbatim from
   ../P3_Agents/seed/fixtures/members.json. olivia.dupont/olivia.dupree
   are P1's own already-existing near-duplicate name pair there -- this
   is PM-03's "two similar assignee names" difficulty with zero
   invention needed on this repo's part.
2. A handful of items carry a source_message_id pointing at a real
   message in ../P3_Agents/seed/fixtures/messages.json for that same
   channel (e.g. PM-014 cites proj-gamma-0192, "the search index is
   blocked, the staging DB migration hasn't run yet."). Those messages'
   own timestamps are the fixture's (mid-2025); this repo's item/sprint/
   commit timestamps are anchored to ANCHOR_DATE below instead -- a
   tracker item tracing back to chat history far older than the sprint
   currently working it is realistic, so the two calendars are
   deliberately not forced to line up.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

from p1.contracts.outcome_record import (
    EvidenceItem,
    OutcomeRecord,
    ParticipationEntry,
    write_outcome,
)

CHANNEL_ID = "19:proj-gamma@thread.tacv2"
CHANNEL_DISPLAY_NAME = "Project Gamma"

# The day PM-03's day-gap difficulties (a stale blocker, a quiet
# assignee) are computed relative to. Falls inside Sprint 13, before
# that sprint's own end_date, so a PM agent reviewing "as of" this date
# is looking at an in-flight sprint, not a closed one.
ANCHOR_DATE = date(2026, 9, 18)

OUTCOME_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "outcomes"

SPRINTS = [
    {"id": "sprint-12", "display_name": "Sprint 12", "start_date": "2026-08-24", "end_date": "2026-09-06"},
    {"id": "sprint-13", "display_name": "Sprint 13", "start_date": "2026-09-07", "end_date": "2026-09-20"},
]

# Verbatim from ../P3_Agents/seed/fixtures/members.json,
# "19:proj-gamma@thread.tacv2" -- see this module's own docstring.
ASSIGNEES = [
    {"id": "noah.becker", "display_name": "Noah Becker"},
    {"id": "aisha.rahman", "display_name": "Aisha Rahman"},
    {"id": "wei.chen", "display_name": "Wei Chen"},
    {"id": "olivia.dupont", "display_name": "Olivia Dupont"},
    {"id": "mateo.silva", "display_name": "Mateo Silva"},
    {"id": "olivia.dupree", "display_name": "Olivia Dupree"},
]

# The tracker adapter's own notion of a "known" status. items.status is
# NOT constrained to this set at the schema level (see 0001_initial.sql)
# -- PM-022 below deliberately holds a value outside it.
CANONICAL_STATUSES = {"backlog", "in_progress", "blocked", "in_review", "done"}

# ---------------------------------------------------------------------------
# Items: PM-01/02's 25-40 tracker items across 2 sprints. PM-014 through
# PM-022 each carry one of PM-03's ten planted difficulties (PM-014/
# PM-015 together are the one difficulty that needs a stale/fresh
# contrast pair); PM-023/PM-024 are the two blockers that DO have a
# matching RISKS entry below (the contrast side of the risk log's own
# "two matching current blockers"); everything else is ordinary sprint
# work, present for volume and so the two sprints read as real ones.
# ---------------------------------------------------------------------------

ITEMS = [
    # -- Sprint 12 (closed): 13 items, mostly shipped.
    {"id": "PM-001", "title": "Design billing sync retry strategy", "status": "done", "sprint_id": "sprint-12", "assignee_id": "olivia.dupont", "created_at": "2026-08-24"},
    {"id": "PM-002", "title": "Implement caching layer eviction policy", "status": "done", "sprint_id": "sprint-12", "assignee_id": "wei.chen", "created_at": "2026-08-24"},
    {"id": "PM-003", "title": "Build auth flow token refresh", "status": "done", "sprint_id": "sprint-12", "assignee_id": "wei.chen", "created_at": "2026-08-25"},
    {"id": "PM-004", "title": "Scaffold onboarding wizard steps", "status": "done", "sprint_id": "sprint-12", "assignee_id": "noah.becker", "created_at": "2026-08-25"},
    {"id": "PM-005", "title": "Build reporting dashboard v1", "status": "done", "sprint_id": "sprint-12", "assignee_id": "mateo.silva", "created_at": "2026-08-26"},
    {"id": "PM-006", "title": "Implement search index base schema", "status": "done", "sprint_id": "sprint-12", "assignee_id": "olivia.dupree", "created_at": "2026-08-26"},
    {"id": "PM-007", "title": "Build retry queue worker", "status": "done", "sprint_id": "sprint-12", "assignee_id": "noah.becker", "created_at": "2026-08-27"},
    {"id": "PM-008", "title": "Build export job pipeline v1", "status": "done", "sprint_id": "sprint-12", "assignee_id": "mateo.silva", "created_at": "2026-08-28"},
    {"id": "PM-009", "title": "Write onboarding wizard integration tests", "status": "done", "sprint_id": "sprint-12", "assignee_id": "aisha.rahman", "created_at": "2026-08-29"},
    {"id": "PM-010", "title": "Add billing sync monitoring dashboard", "status": "done", "sprint_id": "sprint-12", "assignee_id": "olivia.dupont", "created_at": "2026-08-31"},
    {"id": "PM-011", "title": "Document auth flow token lifecycle", "status": "done", "sprint_id": "sprint-12", "assignee_id": "aisha.rahman", "created_at": "2026-09-01"},
    {"id": "PM-012", "title": "Clean up reporting dashboard legacy queries", "status": "done", "sprint_id": "sprint-12", "assignee_id": "wei.chen", "created_at": "2026-09-02"},
    # Not one of PM-03's ten -- an ordinary descope, so Sprint 12 reads
    # like a real closed sprint (not everything planned ships).
    {"id": "PM-013", "title": "Prototype export job streaming mode", "status": "backlog", "sprint_id": "sprint-12", "assignee_id": "mateo.silva", "created_at": "2026-08-24"},

    # -- Sprint 13 (in flight, as of ANCHOR_DATE): 17 items.
    # Difficulty 1/10: a stale blocker (open since 2026-09-14, four days
    # before ANCHOR_DATE) with NO matching RISKS entry below.
    {"id": "PM-014", "title": "Search index blocked on staging DB migration", "status": "blocked", "sprint_id": "sprint-13", "assignee_id": "olivia.dupree", "created_at": "2026-09-08", "blocked_since": "2026-09-14", "source_message_id": "proj-gamma-0192"},
    # Difficulty 1's contrast case: a blocker open only one day (since
    # 2026-09-17) -- genuinely fresh, so its own absence from the risk
    # log is expected, not a gap.
    {"id": "PM-015", "title": "Export job null-case handling blocked pending product decision", "status": "blocked", "sprint_id": "sprint-13", "assignee_id": "wei.chen", "created_at": "2026-09-09", "blocked_since": "2026-09-17", "source_message_id": "proj-gamma-0197"},

    # Difficulty 2/10: moved to done and back to in_progress the same
    # day -- see ITEM_TRANSITIONS. Status here is its current one.
    {"id": "PM-016", "title": "Reporting dashboard export bug", "status": "in_progress", "sprint_id": "sprint-13", "assignee_id": "mateo.silva", "created_at": "2026-09-08"},

    # Difficulty 3/10: aisha.rahman has zero transitions/comments/
    # commits anywhere in this seed on 2026-09-16 or 2026-09-17 (two
    # full days immediately before ANCHOR_DATE), despite last touching
    # this assigned, in-flight item on 2026-09-15 (see ITEM_COMMENTS).
    {"id": "PM-017", "title": "Onboarding wizard copy review", "status": "in_progress", "sprint_id": "sprint-13", "assignee_id": "aisha.rahman", "created_at": "2026-09-08"},

    # Difficulty 4/10: unassigned.
    {"id": "PM-018", "title": "Caching layer PR review", "status": "in_review", "sprint_id": "sprint-13", "assignee_id": None, "created_at": "2026-09-08", "source_message_id": "proj-gamma-0177"},

    # Difficulty 5/10 (a pair): created after Sprint 13's own
    # start_date (2026-09-07) -- added mid-sprint, not part of the
    # original plan.
    {"id": "PM-019", "title": "Fix retry queue duplicate delivery on redelivery", "status": "in_progress", "sprint_id": "sprint-13", "assignee_id": "noah.becker", "created_at": "2026-09-12"},
    {"id": "PM-020", "title": "Add rate limiting to export job endpoint", "status": "in_progress", "sprint_id": "sprint-13", "assignee_id": "wei.chen", "created_at": "2026-09-15"},

    # Difficulty 7/10: never transitioned since creation -- zero rows in
    # ITEM_TRANSITIONS for PM-021, despite COMMITS carrying a commit
    # that names it (the code-host adapter's own edge case: "one item
    # referenced by a commit but never transitioned").
    {"id": "PM-021", "title": "Retry queue backoff jitter", "status": "backlog", "sprint_id": "sprint-13", "assignee_id": "noah.becker", "created_at": "2026-09-08"},

    # Difficulty 8/10: a free-text status outside CANONICAL_STATUSES.
    {"id": "PM-022", "title": "Billing sync vendor API migration", "status": "waiting_on_vendor", "sprint_id": "sprint-13", "assignee_id": "olivia.dupont", "created_at": "2026-09-08", "source_message_id": "proj-gamma-0178"},

    # The risk log's own "two matching current blockers" -- ordinary
    # blockers, each with a RISKS row below pointing back at it.
    {"id": "PM-023", "title": "Auth flow token refresh failing intermittently in staging", "status": "blocked", "sprint_id": "sprint-13", "assignee_id": "wei.chen", "created_at": "2026-09-08", "blocked_since": "2026-09-10"},
    {"id": "PM-024", "title": "Billing sync nightly job missing SLA", "status": "blocked", "sprint_id": "sprint-13", "assignee_id": "olivia.dupont", "created_at": "2026-09-08", "blocked_since": "2026-09-11", "source_message_id": "proj-gamma-0187"},

    # Ordinary in-flight Sprint 13 work -- no planted difficulty, just
    # volume.
    {"id": "PM-025", "title": "Onboarding wizard analytics events", "status": "done", "sprint_id": "sprint-13", "assignee_id": "noah.becker", "created_at": "2026-09-07"},
    {"id": "PM-026", "title": "Search index reindex job monitoring", "status": "done", "sprint_id": "sprint-13", "assignee_id": "olivia.dupree", "created_at": "2026-09-07"},
    {"id": "PM-027", "title": "Export job CSV formatting fix", "status": "in_review", "sprint_id": "sprint-13", "assignee_id": "mateo.silva", "created_at": "2026-09-08"},
    {"id": "PM-028", "title": "Reporting dashboard filter persistence", "status": "in_progress", "sprint_id": "sprint-13", "assignee_id": "wei.chen", "created_at": "2026-09-07"},
    {"id": "PM-029", "title": "Caching layer TTL tuning", "status": "in_progress", "sprint_id": "sprint-13", "assignee_id": "olivia.dupont", "created_at": "2026-09-08"},
    {"id": "PM-030", "title": "Auth flow rate limit headers", "status": "done", "sprint_id": "sprint-13", "assignee_id": "aisha.rahman", "created_at": "2026-09-08"},
]


def _history(item_id: str, steps: list[tuple[str, str]]) -> list[dict]:
    """steps: ordered [(to_status, changed_at), ...], the item's history
    after its initial "backlog" creation state. Returns one row per
    step, each carrying the status transitioned out of."""
    rows = []
    prev_status = "backlog"
    for to_status, changed_at in steps:
        rows.append({"item_id": item_id, "from_status": prev_status, "to_status": to_status, "changed_at": changed_at})
        prev_status = to_status
    return rows


ITEM_TRANSITIONS: list[dict] = [
    *_history("PM-001", [("in_progress", "2026-08-25"), ("done", "2026-08-28")]),
    *_history("PM-002", [("in_progress", "2026-08-25"), ("done", "2026-08-29")]),
    *_history("PM-003", [("in_progress", "2026-08-26"), ("done", "2026-08-30")]),
    *_history("PM-004", [("in_progress", "2026-08-26"), ("done", "2026-08-29")]),
    *_history("PM-005", [("in_progress", "2026-08-27"), ("done", "2026-09-02")]),
    *_history("PM-006", [("in_progress", "2026-08-27"), ("done", "2026-08-31")]),
    *_history("PM-007", [("in_progress", "2026-08-28"), ("done", "2026-09-03")]),
    *_history("PM-008", [("in_progress", "2026-08-29"), ("done", "2026-09-04")]),
    *_history("PM-009", [("in_progress", "2026-08-30"), ("done", "2026-09-03")]),
    *_history("PM-010", [("in_progress", "2026-09-01"), ("done", "2026-09-04")]),
    *_history("PM-011", [("in_progress", "2026-09-02"), ("done", "2026-09-05")]),
    *_history("PM-012", [("in_progress", "2026-09-03"), ("done", "2026-09-06")]),
    # PM-013: no transitions -- descoped straight out of backlog.

    *_history("PM-014", [("in_progress", "2026-09-10"), ("blocked", "2026-09-14")]),
    *_history("PM-015", [("in_progress", "2026-09-13"), ("blocked", "2026-09-17")]),
    # Difficulty 2/10: done, then reopened to in_progress hours later,
    # same calendar day -- needs a time component to show the churn.
    *_history("PM-016", [
        ("in_progress", "2026-09-15"),
        ("done", "2026-09-16T10:00:00"),
        ("in_progress", "2026-09-16T15:30:00"),
    ]),
    *_history("PM-017", [("in_progress", "2026-09-09")]),
    # PM-018: reaches in_review despite being unassigned -- being
    # unassigned doesn't stop work happening, that's what makes it a
    # difficulty rather than an inert row.
    *_history("PM-018", [("in_progress", "2026-09-10"), ("in_review", "2026-09-16")]),
    *_history("PM-019", [("in_progress", "2026-09-13")]),
    *_history("PM-020", [("in_progress", "2026-09-16")]),
    # PM-021: no transitions -- see Difficulty 7/10 above.
    *_history("PM-022", [("in_progress", "2026-09-11"), ("waiting_on_vendor", "2026-09-13")]),
    *_history("PM-023", [("in_progress", "2026-09-09"), ("blocked", "2026-09-10")]),
    *_history("PM-024", [("in_progress", "2026-09-10"), ("blocked", "2026-09-11")]),
    *_history("PM-025", [("in_progress", "2026-09-08"), ("done", "2026-09-12")]),
    *_history("PM-026", [("in_progress", "2026-09-08"), ("done", "2026-09-13")]),
    *_history("PM-027", [("in_progress", "2026-09-09"), ("in_review", "2026-09-17")]),
    *_history("PM-028", [("in_progress", "2026-09-11")]),
    *_history("PM-029", [("in_progress", "2026-09-12")]),
    *_history("PM-030", [("in_progress", "2026-09-09"), ("done", "2026-09-15")]),
]

ITEM_COMMENTS = [
    # Difficulty 3/10's last-touch evidence: aisha.rahman was active on
    # PM-017 on 2026-09-15, then goes quiet for the two days before
    # ANCHOR_DATE (2026-09-16, 2026-09-17) -- see this module's own
    # header note on PM-017.
    {"item_id": "PM-017", "author_id": "aisha.rahman", "body": "Started reviewing copy, about halfway through.", "tags": [], "created_at": "2026-09-15"},
    {"item_id": "PM-014", "author_id": "olivia.dupree", "body": "Blocked -- staging DB migration hasn't run yet, filed with infra.", "tags": ["blocked-reason"], "created_at": "2026-09-14"},
    {"item_id": "PM-023", "author_id": "wei.chen", "body": "Blocked -- token refresh intermittently failing in staging, investigating.", "tags": ["blocked-reason"], "created_at": "2026-09-10"},
]

# ---------------------------------------------------------------------------
# Commits: PM-01/02's "commit history" -- 12 commits across both
# sprints. c8 (no item_ref) is Difficulty 6/10; c9 (references PM-021,
# which is never transitioned -- Difficulty 7/10) is the code-host
# adapter's own paired edge case.
# ---------------------------------------------------------------------------

COMMITS = [
    {"sha": "a1111aa", "author_id": "olivia.dupont", "message": "PM-001: implement exponential backoff for billing sync retries", "item_ref": "PM-001", "committed_at": "2026-08-26"},
    {"sha": "a2222bb", "author_id": "wei.chen", "message": "PM-002: LRU eviction for caching layer", "item_ref": "PM-002", "committed_at": "2026-08-27"},
    {"sha": "a3333cc", "author_id": "wei.chen", "message": "PM-003: refresh token rotation for auth flow", "item_ref": "PM-003", "committed_at": "2026-08-29"},
    {"sha": "a4444dd", "author_id": "noah.becker", "message": "PM-004: onboarding wizard step scaffolding", "item_ref": "PM-004", "committed_at": "2026-08-28"},
    {"sha": "a5555ee", "author_id": "mateo.silva", "message": "PM-005: reporting dashboard v1 initial render", "item_ref": "PM-005", "committed_at": "2026-09-01"},
    {"sha": "a6666ff", "author_id": "noah.becker", "message": "PM-007: retry queue worker skeleton", "item_ref": "PM-007", "committed_at": "2026-09-02"},
    {"sha": "b7777aa", "author_id": "mateo.silva", "message": "PM-016: attempt 1 at reporting dashboard export bug fix", "item_ref": "PM-016", "committed_at": "2026-09-16"},
    # Difficulty 6/10: a commit with no item reference.
    {"sha": "b8888bb", "author_id": "wei.chen", "message": "chore: bump CI runner image to node 20", "item_ref": None, "committed_at": "2026-09-16"},
    # Difficulty 7/10's other half: references PM-021, which
    # ITEM_TRANSITIONS never moves out of backlog.
    {"sha": "b9999cc", "author_id": "noah.becker", "message": "PM-021: add jitter to retry backoff calculation", "item_ref": "PM-021", "committed_at": "2026-09-13"},
    {"sha": "ba000dd", "author_id": "noah.becker", "message": "PM-019: dedupe retry queue delivery by idempotency key", "item_ref": "PM-019", "committed_at": "2026-09-13"},
    {"sha": "bb111ee", "author_id": "wei.chen", "message": "PM-020: add token bucket rate limiter to export job endpoint", "item_ref": "PM-020", "committed_at": "2026-09-16"},
    {"sha": "bc222ff", "author_id": "mateo.silva", "message": "PM-027: fix CSV quoting for export job", "item_ref": "PM-027", "committed_at": "2026-09-17"},
]

# ---------------------------------------------------------------------------
# Commitments: PM-01/02's "delivery commitments" -- cm1 is Difficulty
# 9/10 (a relative due date only); the rest carry a proper ISO date, for
# contrast.
# ---------------------------------------------------------------------------

COMMITMENTS = [
    # Difficulty 9/10: no due_date_iso, only a relative phrase.
    {"member_id": "mateo.silva", "item_id": "PM-016", "text": "I'll have the reporting dashboard export bug fixed by end of week.", "due_date_iso": None, "due_date_text": "end of week", "made_at": "2026-09-15", "source_message_id": None},
    {"member_id": "olivia.dupont", "item_id": "PM-022", "text": "Vendor says the new billing API will be ready 2026-09-25; cutting over once it's live.", "due_date_iso": "2026-09-25", "due_date_text": None, "made_at": "2026-09-12", "source_message_id": None},
    {"member_id": "wei.chen", "item_id": "PM-028", "text": "Filter persistence should land by 2026-09-19.", "due_date_iso": "2026-09-19", "due_date_text": None, "made_at": "2026-09-14", "source_message_id": None},
    {"member_id": "noah.becker", "item_id": "PM-019", "text": "Idempotency-key fix for retry queue duplicates is verifying in staging, done by 2026-09-18.", "due_date_iso": "2026-09-18", "due_date_text": None, "made_at": "2026-09-13", "source_message_id": None},
]

# ---------------------------------------------------------------------------
# Risks: three pre-existing entries. RISK-001/002 match PM-023/PM-024
# (the risk log's own "two matching current blockers"). RISK-003 is
# historical/mitigated and matches no currently-open blocker -- see
# PM-014/PM-015's own comment above for the item side of that gap.
# ---------------------------------------------------------------------------

RISKS = [
    {"id": "RISK-001", "title": "Auth flow token refresh intermittent failures in staging", "description": "Token refresh calls are failing intermittently under load in the staging environment; root cause not yet confirmed.", "severity": "medium", "status": "open", "related_item_id": "PM-023", "opened_at": "2026-09-10"},
    {"id": "RISK-002", "title": "Billing sync nightly job at risk of missing SLA", "description": "The nightly billing sync job has been running past its committed SLA window twice this sprint; vendor-side latency suspected.", "severity": "high", "status": "open", "related_item_id": "PM-024", "opened_at": "2026-09-11"},
    {"id": "RISK-003", "title": "Third-party billing API rate limits may throttle nightly sync during peak season", "description": "Flagged during Sprint 12 planning; mitigated by moving the sync off-peak. No currently-open blocker traces back to this.", "severity": "low", "status": "mitigated", "related_item_id": None, "opened_at": "2026-08-20"},
]


def build_seed(conn: sqlite3.Connection) -> None:
    """Resets and repopulates every seeded table from the static data
    above. Safe to call any number of times -- each call clears the
    tables first, so re-running always lands on the exact same rows
    (the "reproducible" half of D11's own DoD line), the same posture
    P1's own write_outcome() takes toward re-running being safe."""
    conn.execute("PRAGMA foreign_keys = OFF")  # so the DELETE order below doesn't have to respect FKs
    for table in ("item_comments", "item_transitions", "commitments", "commits", "risks", "items", "assignees", "sprints"):
        conn.execute(f"DELETE FROM {table}")
    conn.execute("PRAGMA foreign_keys = ON")

    conn.executemany(
        "INSERT INTO sprints (id, display_name, start_date, end_date) VALUES (:id, :display_name, :start_date, :end_date)",
        SPRINTS,
    )
    conn.executemany(
        "INSERT INTO assignees (id, display_name) VALUES (:id, :display_name)",
        ASSIGNEES,
    )
    conn.executemany(
        """
        INSERT INTO items (id, title, status, sprint_id, assignee_id, created_at, blocked_since, source_message_id)
        VALUES (:id, :title, :status, :sprint_id, :assignee_id, :created_at, :blocked_since, :source_message_id)
        """,
        [
            {"blocked_since": None, "source_message_id": None, **item}
            for item in ITEMS
        ],
    )
    conn.executemany(
        "INSERT INTO item_transitions (item_id, from_status, to_status, changed_at) VALUES (:item_id, :from_status, :to_status, :changed_at)",
        ITEM_TRANSITIONS,
    )
    conn.executemany(
        "INSERT INTO item_comments (item_id, author_id, body, tags, created_at) VALUES (:item_id, :author_id, :body, :tags, :created_at)",
        [{**c, "tags": _json_dumps(c["tags"])} for c in ITEM_COMMENTS],
    )
    conn.executemany(
        "INSERT INTO commits (sha, author_id, message, item_ref, committed_at) VALUES (:sha, :author_id, :message, :item_ref, :committed_at)",
        COMMITS,
    )
    conn.executemany(
        """
        INSERT INTO commitments (member_id, item_id, text, due_date_iso, due_date_text, made_at, source_message_id)
        VALUES (:member_id, :item_id, :text, :due_date_iso, :due_date_text, :made_at, :source_message_id)
        """,
        COMMITMENTS,
    )
    conn.executemany(
        "INSERT INTO risks (id, title, description, severity, status, related_item_id, opened_at) VALUES (:id, :title, :description, :severity, :status, :related_item_id, :opened_at)",
        RISKS,
    )
    conn.commit()


def _json_dumps(value: list[str]) -> str:
    import json

    return json.dumps(value)


# ---------------------------------------------------------------------------
# Outcome records: PM-01/02's "2 P1 outcome records with one missing its
# scope/consent flag" -- constructed directly against P1's own
# OutcomeRecord/EvidenceItem/ParticipationEntry contract (CHN-26) and
# written with P1's own write_outcome(), with zero new serialization
# code. "allowlisted" IS the scope/consent flag the master plan means:
# it's a required bool on the contract, not an optional field that can
# be literally absent, so "missing" means constructing a record with
# allowlisted=False (see ../P3_Agents/src/p1/contracts/outcome_record.py).
#
# Evidence message_ids below are real ids from
# ../P3_Agents/seed/fixtures/messages.json's proj-gamma channel -- see
# this module's own header note on why their (2025) dates aren't forced
# to match these records' own (2026) date field.
# ---------------------------------------------------------------------------

_ROSTER = [a["id"] for a in ASSIGNEES]

OUTCOME_RECORD_ALLOWLISTED = OutcomeRecord(
    schema_version="1.0",
    channel_id=CHANNEL_ID,
    channel_display_name=CHANNEL_DISPLAY_NAME,
    date=date(2026, 9, 16),
    allowlisted=True,
    roster=_ROSTER,
    updates=[
        EvidenceItem(
            message_id="proj-gamma-0180",
            text="Pushed the fix for the billing sync, should resolve the flaky test.",
            quote="Pushed the fix for the billing sync, should resolve the flaky test.",
        ),
        EvidenceItem(
            message_id="proj-gamma-0193",
            text="Deployed the onboarding wizard to staging, looks stable so far.",
            quote="Deployed the onboarding wizard to staging, looks stable so far.",
        ),
    ],
    blockers=[
        EvidenceItem(
            message_id="proj-gamma-0192",
            text="Search index work is blocked: the staging DB migration hasn't run yet.",
            quote="the search index is blocked, the staging DB migration hasn't run yet.",
        ),
    ],
    decisions=[],
    questions=[
        EvidenceItem(
            message_id="proj-gamma-0197",
            text="Open question on whether the export job should handle the null case.",
            quote="Should the export job handle the null case, or is that out of scope?",
        ),
    ],
    participation=[
        ParticipationEntry(member_id="aisha.rahman", state="no_message", evidence_message_ids=[]),
    ],
    generated_at="2026-09-16T22:05:00+00:00",
)

# Difficulty/edge case: allowlisted=False -- the "missing scope/consent
# flag" record. Its fact sections are deliberately empty: the record
# exists (so a consumer can tell the channel was active and who's on
# it) but P1 was not cleared to publish this day's detailed content, so
# there is nothing to hand P2 beyond that. Neither this record nor the
# one above is built via build_outcome_record()/generate_daily_summary()
# -- there's no real message history in this repo's own seed to
# summarize -- they're constructed directly against the same contract
# classes, exactly as P1's own build_outcome_record() itself does
# internally.
OUTCOME_RECORD_NOT_ALLOWLISTED = OutcomeRecord(
    schema_version="1.0",
    channel_id=CHANNEL_ID,
    channel_display_name=CHANNEL_DISPLAY_NAME,
    date=date(2026, 9, 17),
    allowlisted=False,
    roster=_ROSTER,
    updates=[],
    blockers=[],
    decisions=[],
    questions=[],
    participation=[
        ParticipationEntry(member_id="mateo.silva", state="posted_no_update", evidence_message_ids=[]),
    ],
    generated_at="2026-09-17T22:05:00+00:00",
)

OUTCOME_RECORDS = [OUTCOME_RECORD_ALLOWLISTED, OUTCOME_RECORD_NOT_ALLOWLISTED]


def build_outcome_fixtures(output_dir: str | Path = OUTCOME_FIXTURES_DIR) -> list[Path]:
    """Writes both seeded OutcomeRecords via P1's own write_outcome() --
    reused verbatim, not reimplemented. Safe to call any number of
    times: write_outcome() itself overwrites in place (see its own
    docstring in P1)."""
    return [write_outcome(record, output_dir=output_dir) for record in OUTCOME_RECORDS]
