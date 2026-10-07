"""PM-25 golden case 7: nudges never exceed the per-person daily cap, counting P1's too, and a reminder comes before
an escalation, without anything repeating beyond the cap.

Two agents each chase people about late work: P1 reminds the people who have not posted an update, P2 reminds the
people who made a commitment and escalates the ones whose commitment is badly overdue. This case runs P1's real
nudge job and P2's real follow-up against each other on the seed's commitments for three working days, each agent
three times a day (the second and third runs must do nothing), the first agent alternating so each is the one held
back on some day. The seed's commitments and the people P1 chases are fixed, and so is what must happen, worked out
by hand from the rules (EXPECTED below), not read off a run:

  Fri 18 Sep   P1 first, for Wei, Mateo and Noah. P1's reminders to Wei and Mateo go out; its first ever reminder to
               Noah waits for a person's approval and is never approved, so it is not delivered and does not count. P2 is
               held back for Wei and Mateo (P1 got there first) and reminds Noah, who is due today: his first reminder
               from P2 also waits for approval, which a person gives. Noah's overdue commitment is queued behind it:
               one reminder a person a day, even while the first is still waiting.
  Mon 21 Sep   P2 first: commitments more than 3 days overdue that never had a reminder get one (Noah, Wei, Dupree,
               Dupont). P1 then chases Wei, Mateo and Dupont: held back for Wei and Dupont, reminds Mateo.
  Tue 22 Sep   P2 first. It plans a reminder to Mateo (his commitment is now 4 days overdue and was never reminded; it
               is P2's first ever to him, so it waits for a person) and escalations to the lead for Wei, Dupree and Dupont
               (each was reminded the day before; the first escalation about a person waits for a person). P1 then
               reminds Dupree and Mateo, and delivers. At the end of the day a person approves what waits: the
               escalations go out, and the reminder to Mateo is REFUSED at the moment of sending, because P1 reminded him
               that day. Noah's own overdue commitments are not escalated: he is the lead, and there is nobody else to tell.

What is measured is what was DELIVERED, recorded at the publisher of each agent, so a ledger that lies cannot make
the numbers look right. The rules, each a plain function of the deliveries (tested on hand-made logs):

  cap_breaches          a person (the same person under P1's Graph id and P2's roster id) reminded more than the shared
                        cap in one day, by both agents together
  order_violations      an escalation delivered without a reminder to the person about that commitment on an EARLIER day
  repeats               a second reminder or a second escalation about one commitment, or an exact resend
  schedule_mismatches   delivered vs the hand-labelled schedule (the symmetric difference)
  ledger_mismatches     the two ledgers the cap reads say something other than what was delivered

and a control, the same estate with the shared cap switched off on both sides, must show people chased twice: so zero
breaches is not a number that cannot be anything else.

tests/unit/test_eval_pm25.py breaks each guard in turn (P2 blind to P1, P1 blind to P2, a send never written to the
ledger, an escalation that ignores the rule that the person hears first) and checks the matching number notices.
"""

from __future__ import annotations

import os
import re
import tempfile
from collections import Counter
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

from spine.approval.proposals import APPLIED, ProposalStore
from spine.eval.cases import (
    GoldenCase,
    GoldenCaseRegistry,
    MetricResult,
    at_least,
    at_most,
)

from pm.adapters.tracker import ItemNotFoundError, TrackerMock
from pm.approval import service
from pm.approval.service import ApprovalPolicy
from pm.commitments import followup as fu
from pm.commitments.cap import SharedNudgeCap
from pm.commitments.followup import FollowupSettings, run_followups
from pm.eval.pristine import build_pristine_database

CAP = 1
RUNS_PER_AGENT_PER_DAY = 3
APPROVER = "sharon.silva"
LEAD = "noah.becker"
PRIMED_DAY = "2026-09-01"  # everyone but one person has been reminded before by each agent: the steady state, no first-time approval
NEW_TO_BOTH = "noah.becker"  # the exception: neither agent has reminded him yet, so each one's first reminder to him waits for a person
NEW_TO_P2 = frozenset({NEW_TO_BOTH, "mateo.silva"})  # P2 has never reminded these two, so its first reminder to each waits for a person to approve it
NEW_TO_P1 = frozenset({NEW_TO_BOTH})
SETTINGS = FollowupSettings(
    channel_id="19:proj-gamma@thread.tacv2", timezone="Asia/Colombo", working_days=["Mon", "Tue", "Wed", "Thu", "Fri"], non_working_dates=[],
    nudge_days_before_due=1, escalation_threshold_days=3, lead_id=LEAD, exceptions=frozenset(), cap_per_person_per_day=CAP,
)

P1_CHANNEL = "p1-channel"
# P2's roster id -> the Microsoft Graph user id P1 knows the same person by. Only the names match across the two.
P1_IDS = {
    "wei.chen": "aad-wei-0001", "mateo.silva": "aad-mateo-0002", "olivia.dupont": "aad-dupont-0003",
    "olivia.dupree": "aad-dupree-0004", "noah.becker": "aad-noah-0005",
}
PERSON_OF_P1_ID = {p1: p2 for p2, p1 in P1_IDS.items()}


@dataclass(frozen=True)
class Day:
    iso: str
    first: str  # which agent runs first that day: "p1" | "p2"
    p1_chases: tuple[str, ...]  # who P1's ledger says has not posted an update that day (P2 roster ids)

    @property
    def moment(self) -> datetime:
        y, m, d = (int(x) for x in self.iso.split("-"))
        return datetime(y, m, d, 3, 30, tzinfo=timezone.utc)  # 09:00 in Colombo


DAYS = (
    Day("2026-09-18", "p1", ("wei.chen", "mateo.silva", NEW_TO_BOTH)),
    Day("2026-09-21", "p2", ("wei.chen", "mateo.silva", "olivia.dupont")),
    Day("2026-09-22", "p2", ("olivia.dupree", "mateo.silva")),
)

# (agent, day, person the reminder or escalation is ABOUT, kind), worked out by hand from the rules, see the docstring.
EXPECTED: frozenset[tuple[str, str, str, str]] = frozenset({
    ("p1", "2026-09-18", "wei.chen", "nudge"), ("p1", "2026-09-18", "mateo.silva", "nudge"), ("p2", "2026-09-18", "noah.becker", "nudge"),
    ("p2", "2026-09-21", "noah.becker", "nudge"), ("p2", "2026-09-21", "wei.chen", "nudge"),
    ("p2", "2026-09-21", "olivia.dupree", "nudge"), ("p2", "2026-09-21", "olivia.dupont", "nudge"), ("p1", "2026-09-21", "mateo.silva", "nudge"),
    ("p1", "2026-09-22", "olivia.dupree", "nudge"), ("p1", "2026-09-22", "mateo.silva", "nudge"),
    ("p2", "2026-09-22", "wei.chen", "escalation"), ("p2", "2026-09-22", "olivia.dupree", "escalation"), ("p2", "2026-09-22", "olivia.dupont", "escalation"),
})


@dataclass(frozen=True)
class Delivery:
    """One direct message that actually went out, as seen at the sending agent's publisher."""

    agent: str  # "p1" | "p2"
    day: str
    recipient: str  # who it was sent to (a Graph id for P1, a roster id for P2)
    person: str  # who it is about: the person reminded, or the owner of the commitment escalated
    kind: str  # "nudge" | "escalation"
    commitment_id: int | None
    content: str


@dataclass
class Run:
    deliveries: list[Delivery] = field(default_factory=list)
    held: set[tuple[str, str, str]] = field(default_factory=set)  # (agent, day, person) held back because the OTHER agent already reminded them
    refused_at_send: set[tuple[str, str, str]] = field(default_factory=set)  # (agent, day, person) approved by a person, then refused at the moment of sending: the other agent reminded them first
    ledger: dict[tuple[str, str, str], int] = field(default_factory=dict)  # (agent, person, day) -> reminders the agent's ledger says were sent
    runs_per_agent_per_day: int = RUNS_PER_AGENT_PER_DAY
    cap: int = CAP


# --- the rules, as plain functions of the deliveries -----------------------------------------------------------------------


def cap_breaches(deliveries: Iterable[Delivery], cap: int = CAP) -> list[tuple[str, str, int]]:
    """(person, day, how many) for every person reminded more than `cap` times in a day, both agents together.
    Escalations go to the lead, about someone else's commitment: they are not reminders and do not count."""
    counts = Counter((d.person, d.day) for d in deliveries if d.kind == "nudge")
    return sorted((person, day, n) for (person, day), n in counts.items() if n > cap)


def order_violations(deliveries: Iterable[Delivery]) -> list[str]:
    """An escalation about a commitment needs a reminder about that commitment delivered on an earlier day."""
    deliveries = list(deliveries)
    out = []
    for e in (d for d in deliveries if d.kind == "escalation"):
        earlier = [n for n in deliveries if n.kind == "nudge" and n.commitment_id == e.commitment_id and n.day < e.day]
        if not earlier:
            out.append(f"commitment {e.commitment_id} about {e.person} escalated on {e.day} with no earlier-day reminder to them")
    return out


def repeats(deliveries: Iterable[Delivery]) -> list[str]:
    """More than one reminder, or more than one escalation, for one commitment; or the very same message sent twice."""
    deliveries = list(deliveries)
    out = []
    by_commitment = Counter((d.commitment_id, d.kind) for d in deliveries if d.commitment_id is not None)
    out += [f"commitment {cid}: {n} {kind} messages" for (cid, kind), n in sorted(by_commitment.items(), key=str) if n > 1]
    exact = Counter((d.agent, d.day, d.recipient, d.content) for d in deliveries)
    out += [f"sent {n} times: {agent} {day} to {recipient}" for (agent, day, recipient, _), n in sorted(exact.items()) if n > 1]
    return out


def schedule_mismatches(deliveries: Iterable[Delivery], expected: frozenset = EXPECTED) -> list[tuple]:
    """What was delivered and was not expected, and what was expected and was not delivered."""
    delivered = {(d.agent, d.day, d.person, d.kind) for d in deliveries}
    return sorted(delivered ^ set(expected))


def ledger_mismatches(run: Run) -> list[tuple]:
    """(agent, person, day, ledger says, delivered) wherever a ledger's count of sent reminders is not what was delivered.
    Each agent's cap reads the OTHER's ledger, so a ledger that undercounts is how a person gets chased twice."""
    delivered = Counter((d.agent, d.person, d.day) for d in run.deliveries if d.kind == "nudge")
    keys = set(delivered) | set(run.ledger)
    return sorted((agent, person, day, run.ledger.get((agent, person, day), 0), delivered.get((agent, person, day), 0))
                  for agent, person, day in keys if run.ledger.get((agent, person, day), 0) != delivered.get((agent, person, day), 0))


# --- running the two agents against each other -----------------------------------------------------------------------------


class _Publisher:
    """An agent's publisher for one day: whatever reaches post_direct_message is a message that went out."""

    def __init__(self, sink: list[tuple[str, str, str, str]], agent: str, day: str) -> None:
        self._sink, self._agent, self._day = sink, agent, day

    def post_direct_message(self, member_id: str, content: str) -> dict:
        self._sink.append((self._agent, self._day, member_id, content))
        return {"ok": True}


@contextmanager
def _environment(p1_db: Path, p2_db: Path, *, shared_cap: bool) -> Iterator[None]:
    """P2 finds P1's ledger from P1_DB_PATH; P1 finds the shared cap and P2's ledger from its own variables. Restored after."""
    names = ("P1_DB_PATH", "P1_SHARED_NUDGE_CAP_PER_DAY", "P1_PEER_NUDGE_LEDGERS")
    before = {n: os.environ.get(n) for n in names}
    try:
        os.environ["P1_DB_PATH"] = str(p1_db)
        os.environ.pop("P1_SHARED_NUDGE_CAP_PER_DAY", None)
        os.environ.pop("P1_PEER_NUDGE_LEDGERS", None)
        if shared_cap:
            os.environ["P1_SHARED_NUDGE_CAP_PER_DAY"] = str(CAP)
            os.environ["P1_PEER_NUDGE_LEDGERS"] = str(p2_db)
        yield
    finally:
        for name, value in before.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _build_p1(path: Path, names: dict[str, str]) -> None:
    from p1.storage.db import get_connection as p1_connection
    from p1.storage.db import init_db
    from p1.storage.nudges_repo import NudgeStore

    init_db(str(path))
    conn = p1_connection(str(path))
    try:
        conn.execute("INSERT INTO channels (id, display_name, allowlisted) VALUES (?, 'P1 channel', 1)", (P1_CHANNEL,))
        for person, p1_id in P1_IDS.items():
            conn.execute("INSERT INTO members (id, display_name) VALUES (?, ?)", (p1_id, names[person]))
        conn.commit()
    finally:
        conn.close()
    for person, p1_id in P1_IDS.items():
        if person in NEW_TO_P1:
            continue  # never reminded by P1: its first reminder waits for a person, and is never approved here
        NudgeStore(str(path)).record(channel_id=P1_CHANNEL, member_id=p1_id, date=PRIMED_DAY, idempotency_key=f"p1-old:{p1_id}", proposal_id="primed")
        NudgeStore(str(path)).mark_sent(idempotency_key=f"p1-old:{p1_id}", sent_at=f"{PRIMED_DAY}T09:00:00+00:00")


def _p1_config():
    from datetime import time

    from p1.config.schema import ChannelConfig

    return ChannelConfig(
        channel_id=P1_CHANNEL, display_name="P1 channel", roster=list(P1_IDS.values()), update_window_start=time(9, 0),
        update_window_end=time(11, 0), timezone="Asia/Colombo", working_days=["Mon", "Tue", "Wed", "Thu", "Fri"],
        daily_digest_time=time(9, 0), weekly_digest_day="Fri", weekly_digest_time=time(16, 0), channel_owner_id=P1_IDS[LEAD],
        nudge_enabled=True, nudge_cap_per_day=CAP,
    )


def _classify(sink: list, db: Path) -> list[Delivery]:
    """Turn each message that went out into a Delivery: P1's are reminders to the person; P2's say what they are in the
    proposal they were sent from (a reminder to the owner, or an escalation to the lead about the owner's commitment)."""
    by_message: dict[tuple[str, str], tuple[str, int | None, str]] = {}
    for proposal in ProposalStore(db).list_by_status(APPLIED):
        payload = proposal.payload
        if "recipient_id" in payload:
            by_message[(payload["recipient_id"], payload.get("content", ""))] = (
                "escalation" if proposal.type.endswith("escalation") else "nudge", payload.get("commitment_id"), payload.get("member_id", ""),
            )
    out = []
    for agent, day, recipient, content in sink:
        if agent == "p1":
            out.append(Delivery("p1", day, recipient, PERSON_OF_P1_ID.get(recipient, recipient), "nudge", None, content))
        else:
            kind, commitment_id, person = by_message.get((recipient, content), ("unknown", None, recipient))
            out.append(Delivery("p2", day, recipient, person, kind, commitment_id, content))
    return out


def _ledger_counts(p1_db: Path, p2_db: Path) -> dict[tuple[str, str, str], int]:
    import sqlite3

    counts: Counter = Counter()
    for agent, path, to_person in (("p1", p1_db, lambda m: PERSON_OF_P1_ID.get(m, m)), ("p2", p2_db, lambda m: m)):
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            for member, day in conn.execute("SELECT member_id, date FROM nudges WHERE sent_at IS NOT NULL AND date >= ?", (DAYS[0].iso,)):
                counts[(agent, to_person(member), day)] += 1
        finally:
            conn.close()
    return dict(counts)


def run_scenario(directory: Path, *, shared_cap: bool = True) -> Run:
    """Both agents, three working days, each agent three times a day. With shared_cap=False the cap is off on both
    sides: each agent still keeps to its own limit, but neither can see the other (the control)."""
    from p1.nudges.nudge_job import run_nudge_job
    from p1.participation.ledger import NO_MESSAGE, ParticipationRecord

    p2_db = build_pristine_database(directory)
    p1_db = directory / "p1.db"
    items = TrackerMock(db_path=p2_db)
    names = {a.id: a.display_name for a in items.list_assignees()}
    _build_p1(p1_db, names)
    primed = SharedNudgeCap(db_path=p2_db, cap=CAP, p1_db_path=p1_db)
    for person in P1_IDS:
        if person in NEW_TO_P2:
            continue  # never reminded by P2: its first reminder waits for a person to approve it
        primed.record(member_id=person, day=PRIMED_DAY, commitment_id=None, proposal_id=None, key=f"p2-old:{person}")
        primed.mark_sent(f"p2-old:{person}", sent_at=f"{PRIMED_DAY}T03:00:00+00:00", day=PRIMED_DAY)

    def item_status(item_id: str) -> str | None:
        try:
            return items.get_item(item_id).status
        except ItemNotFoundError:
            return None

    policy = ApprovalPolicy(approver_ids=frozenset({APPROVER}))
    config, run, sink = _p1_config(), Run(), []

    def p1_pass(day: Day) -> None:
        records = [ParticipationRecord(channel_id=P1_CHANNEL, member_id=P1_IDS[p], date=day.iso, state=NO_MESSAGE, evidence_message_ids=())
                   for p in day.p1_chases]
        for _ in range(RUNS_PER_AGENT_PER_DAY):
            publisher = _Publisher(sink, "p1", day.iso)
            for result in run_nudge_job(P1_CHANNEL, config, publisher, day=date.fromisoformat(day.iso), db_path=str(p1_db), ledger_records=records):
                if result.status == "cap_reached" and re.search(r"[1-9]\d* from another agent", result.detail or ""):
                    run.held.add(("p1", day.iso, PERSON_OF_P1_ID.get(result.member_id, result.member_id)))

    def p2_pass(day: Day, waiting: dict[str, str]) -> None:
        for _ in range(RUNS_PER_AGENT_PER_DAY):
            publisher = _Publisher(sink, "p2", day.iso)
            results = run_followups(moment=day.moment, settings=SETTINGS, publisher=publisher, db_path=p2_db, policy=policy,
                                    item_status=item_status, names=names)
            for result in results:
                if result.status == fu.CAP_REACHED and re.search(r"[1-9]\d* from P1", result.detail or ""):
                    run.held.add(("p2", day.iso, result.member_id))
                if result.status == fu.AWAITING_APPROVAL and result.proposal_id:
                    waiting[result.proposal_id] = result.member_id

    def approve_what_waits(day: Day, waiting: dict[str, str]) -> None:
        """A person approves whatever waits for one (a first reminder, a first escalation) at the end of the day, after both
        agents have run: so a reminder approved late meets the other agent's reminder already delivered that day."""
        publisher = _Publisher(sink, "p2", day.iso)
        for proposal_id, person in waiting.items():
            outcome = service.approve_and_send(proposal_id, approver_id=APPROVER, publisher=publisher, policy=policy,
                                               db_path=p2_db, now=day.moment)
            if outcome.outcome == service.REFUSED and re.search(r"[1-9]\d* from P1", outcome.detail or ""):
                run.refused_at_send.add(("p2", day.iso, person))

    patches = [] if shared_cap else [mock.patch.object(SharedNudgeCap, "_sent_by_p1", lambda self, member_id, day: (0, "read"))]
    with _environment(p1_db, p2_db, shared_cap=shared_cap):
        for patch in patches:
            patch.start()
        try:
            for day in DAYS:
                waiting: dict[str, str] = {}
                for agent in (day.first, "p2" if day.first == "p1" else "p1"):
                    p1_pass(day) if agent == "p1" else p2_pass(day, waiting)
                approve_what_waits(day, waiting)
        finally:
            for patch in patches:
                patch.stop()
        run.ledger = _ledger_counts(p1_db, p2_db)
    run.deliveries = _classify(sink, p2_db)
    return run


# --- report and metrics -------------------------------------------------------------------------------------------------------


def format_report(run: Run, control: Run) -> str:
    lines = [
        "Golden case 7 -- the shared nudge cap and the order of reminder and escalation",
        f"  P1's real nudge job and P2's real follow-up against each other, 3 working days, each agent {run.runs_per_agent_per_day} times a day, cap {run.cap} a person a day;",
        "  measured from the messages each agent actually sent, not from its ledger",
    ]
    for day in DAYS:
        todays = [d for d in run.deliveries if d.day == day.iso]
        nudges = ", ".join(f"{d.agent}:{d.person}" for d in todays if d.kind == "nudge") or "none"
        escalations = ", ".join(d.person for d in todays if d.kind == "escalation") or "none"
        held = ", ".join(f"{a}:{p}" for a, dd, p in sorted(run.held) if dd == day.iso) or "none"
        refused = ", ".join(f"{a}:{p}" for a, dd, p in sorted(run.refused_at_send) if dd == day.iso) or "none"
        lines.append(f"  {day.iso} ({day.first} first)  reminders sent: {nudges}  |  held back by the other agent: {held}  |  refused at sending: {refused}  |  escalations to the lead: {escalations}")
    breaches, violations, repeated = cap_breaches(run.deliveries, run.cap), order_violations(run.deliveries), repeats(run.deliveries)
    lines += [
        f"  people reminded more than {run.cap} time(s) in a day by both agents together: {len(breaches)}",
        f"  escalations without an earlier-day reminder to the person: {len(violations)}",
        f"  reminders or escalations repeated beyond one per commitment: {len(repeated)}",
        f"  delivered vs the hand-labelled schedule ({len(EXPECTED)} entries): {len(schedule_mismatches(run.deliveries))} differences",
        f"  ledgers vs what was delivered: {len(ledger_mismatches(run))} differences",
        f"  control, shared cap off on both sides: {len(cap_breaches(control.deliveries, control.cap))} person-day(s) chased more than {control.cap} time(s)",
    ]
    lines += [f"      {person} on {day}: {n} reminders" for person, day, n in cap_breaches(control.deliveries, control.cap)]
    return "\n".join(lines)


def metrics(run: Run, control: Run) -> list[MetricResult]:
    breaches, violations, repeated = len(cap_breaches(run.deliveries, run.cap)), len(order_violations(run.deliveries)), len(repeats(run.deliveries))
    mismatches, ledgers = len(schedule_mismatches(run.deliveries)), len(ledger_mismatches(run))
    held, escalations = len(run.held), sum(1 for d in run.deliveries if d.kind == "escalation")
    control_breaches = len(cap_breaches(control.deliveries, control.cap))
    return [
        MetricResult("GC7-cap-breach-count", "person-days where both agents together reminded someone more than the shared cap (hard zero)",
                     breaches, 0, "at_most", at_most(breaches, 0), f"{sum(1 for d in run.deliveries if d.kind == 'nudge')} reminders over 3 days, cap {run.cap}"),
        MetricResult("GC7-escalation-order-violation-count", "escalations delivered without an earlier-day reminder to the person about that commitment (hard zero)",
                     violations, 0, "at_most", at_most(violations, 0), f"{escalations} escalations checked"),
        MetricResult("GC7-repeat-count", "reminders or escalations repeated beyond one per commitment, after three runs a day by each agent (hard zero)",
                     repeated, 0, "at_most", at_most(repeated, 0), f"{run.runs_per_agent_per_day} runs per agent per day"),
        MetricResult("GC7-schedule-mismatch-count", "differences between what was delivered and the hand-labelled schedule (hard zero)",
                     mismatches, 0, "at_most", at_most(mismatches, 0), f"{len(EXPECTED)} hand-labelled entries"),
        MetricResult("GC7-ledger-delivery-mismatch-count", "person-days where an agent's ledger (the one the other agent's cap reads) differs from what it delivered (hard zero)",
                     ledgers, 0, "at_most", at_most(ledgers, 0), "P1's and P2's nudge ledgers against the messages sent"),
        MetricResult("GC7-held-back-by-the-other-agent-count", "reminders held back only because the OTHER agent had already reminded the person (at least the four hand-labelled)",
                     held, 4, "at_least", at_least(held, 4), "both directions: P2 held for P1 on Friday, P1 held for P2 on Monday"),
        MetricResult("GC7-refused-at-send-count", "approved reminders refused at the moment of sending because the other agent had reminded the person that day (at least the one hand-labelled)",
                     len(run.refused_at_send), 1, "at_least", at_least(len(run.refused_at_send), 1), "P2's first reminder to Mateo, approved at the end of Tuesday, after P1's"),
        MetricResult("GC7-escalations-delivered-count", "escalations delivered, so the order rule was exercised (at least the three hand-labelled)",
                     escalations, 3, "at_least", at_least(escalations, 3), "to the lead, each the day after the person's reminder"),
        MetricResult("GC7-control-breach-count", "person-days chased more than the cap by the same estate with the shared cap off (at least 1: zero breaches must not be a number that cannot be anything else)",
                     control_breaches, 1, "at_least", at_least(control_breaches, 1), "the control run"),
    ]


def measure_gc7() -> list[MetricResult]:
    with tempfile.TemporaryDirectory(prefix="pm_gc7_") as tmp, tempfile.TemporaryDirectory(prefix="pm_gc7_control_") as control_tmp:
        return metrics(run_scenario(Path(tmp)), run_scenario(Path(control_tmp), shared_cap=False))


def report() -> str:
    with tempfile.TemporaryDirectory(prefix="pm_gc7_report_") as tmp, tempfile.TemporaryDirectory(prefix="pm_gc7_report_control_") as control_tmp:
        return format_report(run_scenario(Path(tmp)), run_scenario(Path(control_tmp), shared_cap=False))


def register(registry: GoldenCaseRegistry) -> None:
    registry.register(
        GoldenCase(
            case_id="GC7",
            description="Nudge cap and escalation order: P1's and P2's reminders together never exceed the per-person daily cap, "
                        "and a person is reminded before the lead is told, without repeats",
            measure_fn=measure_gc7,
        )
    )
