"""Commitment follow-up: a reminder before the due date, an overdue record after it, an escalation to the lead beyond a threshold.

For every open commitment, on each working day (arithmetic and rules in Python; the wording is fixed templates, no model):

  due within the next N days   a reminder to the person, once, as a direct message through P1's publish adapter
  past its due date            an OVERDUE record, once (nothing is sent: a record that it slipped, and when)
  overdue by MORE than T days  an escalation to the lead with the evidence, once

Guards, each of them real code with its own test:

- the shared per-person per-day cap (pm.commitments.cap): a person already reminded today, by this agent or by P1, is
  not reminded again; checked when planned and again at send time; fail closed if P1's ledger cannot be read;
- the person is told first: a lead is only told about an overdue commitment after the person was reminded on an
  earlier day (an overdue commitment never reminded gets its reminder first, and the lead hears a day later);
- the first reminder this agent ever sends to a person, and the first escalation about a person, wait for a person to
  approve them; after that they go out unattended, within the cap, as a recorded automatic approval;
- nobody on the project's exceptions list (on leave) is reminded or escalated about; nothing is sent on a non-working day;
- a commitment that is done (its item reached `done`) or closed is never chased; the commitment's own status is the truth;
- each follow-up happens once per commitment (a database constraint) however many times this runs.

T and N come from configuration, never from a literal in this module: P1's channel config supplies the lead
(`channel_owner_id`), the escalation threshold (`escalation_threshold_days`) and the cap (`nudge_cap_per_day`), and
environment variables override them for this agent.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from spine.approval.proposals import APPLIED, APPROVED, PENDING, REJECTED, ProposalStore
from spine.config.calendar import is_working_day

from pm.approval import service
from pm.approval.proposals import ESCALATION_PROPOSAL_TYPE, NUDGE_PROPOSAL_TYPE
from pm.commitments import messages
from pm.commitments.ageing import effective_due, is_open
from pm.commitments.cap import (
    SharedCapUnavailableError,
    SharedNudgeCap,
    cap_from_environment,
)
from pm.commitments.store import (
    ESCALATED,
    FULFILLED,
    NUDGED,
    OPEN,
    OVERDUE,
    CommitmentTracker,
    TrackedCommitment,
)
from pm.scheduling.config import P1_CHANNEL_CONFIG_DIR
from pm.seed.build import CHANNEL_ID
from pm.storage.db import DEFAULT_DB_PATH

ENV_NUDGE_DAYS = "PM_COMMITMENT_NUDGE_DAYS_BEFORE"
ENV_ESCALATION_DAYS = "PM_COMMITMENT_ESCALATION_DAYS"
ENV_LEAD = "PM_LEAD_ID"
DEFAULT_NUDGE_DAYS_BEFORE = 1

# What happened to one commitment in a run.
SENT, AWAITING_APPROVAL, REJECTED_STATUS, REFUSED, SEND_FAILED = "sent", "awaiting_approval", "rejected", "refused", "send_failed"
CAP_REACHED, CAP_UNAVAILABLE, QUEUED = "cap_reached", "cap_unavailable", "queued_behind_another_reminder"
EXCLUDED, NON_WORKING_DAY, NOT_DUE, NO_DATE, ALREADY_DONE, RECORDED = "excluded", "skipped_non_working_day", "not_due", "no_date", "already_done", "recorded"
NO_LEAD, WAITING, FULFILLED_STATUS, WOULD_SEND, WOULD_AWAIT = "no_lead", "waiting_a_day", "fulfilled", "would_send", "would_await_approval"


def _whole_number(raw: str, name: str) -> int:
    if not raw.isdigit():
        raise ValueError(f"{name} must be a whole number of days, not {raw!r}")
    return int(raw)


@dataclass(frozen=True)
class FollowupSettings:
    channel_id: str
    timezone: str
    working_days: list[str]
    non_working_dates: list[date]
    nudge_days_before_due: int
    escalation_threshold_days: int
    lead_id: str | None
    exceptions: frozenset[str]
    cap_per_person_per_day: int


def load_followup_settings(
    channel_id: str = CHANNEL_ID, *, env: Mapping[str, str] | None = None, config_dir: str | Path = P1_CHANNEL_CONFIG_DIR,
) -> FollowupSettings:
    """From P1's channel config for this project (the lead, the escalation threshold, the cap, who is on leave),
    overridden by environment variables. Nothing here is a number baked into the code."""
    from p1.config.loader import ChannelConfigStore

    env = os.environ if env is None else env
    config = ChannelConfigStore(config_dir).get_channel_config(channel_id)
    nudge_raw, escalation_raw = (env.get(ENV_NUDGE_DAYS) or "").strip(), (env.get(ENV_ESCALATION_DAYS) or "").strip()
    return FollowupSettings(
        channel_id=channel_id, timezone=config.timezone, working_days=list(config.working_days),
        non_working_dates=list(config.non_working_dates),
        nudge_days_before_due=_whole_number(nudge_raw, ENV_NUDGE_DAYS) if nudge_raw else DEFAULT_NUDGE_DAYS_BEFORE,
        escalation_threshold_days=_whole_number(escalation_raw, ENV_ESCALATION_DAYS) if escalation_raw else config.escalation_threshold_days,
        lead_id=(env.get(ENV_LEAD) or "").strip() or config.channel_owner_id,
        exceptions=frozenset(e.member_id for e in config.exceptions),
        cap_per_person_per_day=cap_from_environment(config.nudge_cap_per_day, env),
    )


@dataclass(frozen=True)
class FollowupResult:
    commitment_id: int
    member_id: str
    action: str  # nudge | overdue | escalation | fulfilled | none
    status: str
    detail: str = ""
    proposal_id: str | None = None


def run_followups(
    *,
    moment: datetime,
    settings: FollowupSettings,
    publisher,
    db_path: str | Path = DEFAULT_DB_PATH,
    policy: service.ApprovalPolicy | None = None,
    item_status: Callable[[str], str | None] = lambda _id: None,
    names: dict[str, str] | None = None,
    dry_run: bool = False,
) -> list[FollowupResult]:
    """One pass over every open commitment as of `moment`. Idempotent: safe to run any number of times in a day.
    With dry_run nothing is written or sent: each result says what WOULD happen. P1's ledger is found from the
    environment (P1_DB_PATH) here AND when a reminder is sent: one place, so planning and sending cannot disagree."""
    return _Run(moment, settings, publisher, db_path, policy, item_status, names or {}, dry_run).execute()


class _Run:
    def __init__(self, moment, settings, publisher, db_path, policy, item_status, names, dry_run):
        self.moment, self.settings, self.publisher, self.db_path = moment, settings, publisher, db_path
        self.policy = policy if policy is not None else service.ApprovalPolicy()
        self.item_status, self.names, self.dry_run = item_status, names, dry_run
        self.tracker, self.store = CommitmentTracker(db_path), ProposalStore(db_path)
        self.cap = SharedNudgeCap(db_path=db_path, cap=settings.cap_per_person_per_day)
        self.today: date = moment.astimezone(ZoneInfo(settings.timezone)).date()
        self.today_iso = self.today.isoformat()
        self.working = is_working_day(self.today, settings)
        self.queued_today: set[str] = set()  # people already reminded (or queued) in THIS run

    def execute(self) -> list[FollowupResult]:
        results: list[FollowupResult] = []
        for commitment in self.tracker.list(status=OPEN):
            results += self._one(commitment)
        return results

    def _name(self, member_id: str) -> str | None:
        return self.names.get(member_id)

    def _one(self, c: TrackedCommitment) -> list[FollowupResult]:
        if not is_open(c, self.item_status):
            if not self.dry_run:
                self.tracker.close(c.id, status=FULFILLED, day=self.today_iso, detail=f"{c.item_id} is done")
            return [FollowupResult(c.id, c.member_id, "fulfilled", FULFILLED_STATUS, f"{c.item_id} is done")]
        due_text = effective_due(c)
        if due_text is None:
            return [FollowupResult(c.id, c.member_id, "none", NO_DATE, "it gave no day to hold it to")]
        if c.member_id in self.settings.exceptions:
            return [FollowupResult(c.id, c.member_id, "none", EXCLUDED, f"{c.member_id} is on the exceptions list")]

        delta = (date.fromisoformat(due_text) - self.today).days
        events = {e["kind"]: e for e in self.tracker.events(c.id)}
        if 0 <= delta <= self.settings.nudge_days_before_due:
            return [self._nudge(c, due_text, delta, events)]
        if delta < 0:
            return self._overdue(c, due_text, -delta, events)
        return [FollowupResult(c.id, c.member_id, "none", NOT_DUE, f"due {due_text}, in {delta} day(s)")]

    # --- the overdue record, then the escalation --------------------------------------------------------------------------------

    def _overdue(self, c, due_text, overdue_days, events) -> list[FollowupResult]:
        out = []
        if OVERDUE not in events:
            if not self.dry_run:
                self.tracker.record_event(c.id, OVERDUE, self.today_iso, f"{overdue_days} day(s) overdue: due {due_text}")
            out.append(FollowupResult(c.id, c.member_id, "overdue", RECORDED, f"due {due_text}, {overdue_days} day(s) overdue"))
        if overdue_days > self.settings.escalation_threshold_days:
            out.append(self._escalate(c, due_text, overdue_days, events))
        return out

    def _escalate(self, c, due_text, overdue_days, events) -> FollowupResult:
        if ESCALATED in events:
            return FollowupResult(c.id, c.member_id, "escalation", ALREADY_DONE, f"the lead was told on {events[ESCALATED]['day']}")
        nudged = events.get(NUDGED)
        if nudged is None:
            late = self._nudge(c, due_text, -overdue_days, events)  # the person hears first, the lead a day later
            return FollowupResult(c.id, c.member_id, "nudge", late.status, f"never reminded, so reminded now and the lead hears later: {late.detail}", late.proposal_id)
        if nudged["day"] >= self.today_iso:
            return FollowupResult(c.id, c.member_id, "escalation", WAITING, "reminded today: the lead hears tomorrow if it is still open")
        lead = self.settings.lead_id
        if not lead or lead == c.member_id:
            return FollowupResult(c.id, c.member_id, "escalation", NO_LEAD, "there is no lead other than the owner to tell")
        if not self.working:
            return FollowupResult(c.id, c.member_id, "escalation", NON_WORKING_DAY, "not a working day")
        content = messages.escalation_text(
            c, owner_name=self._name(c.member_id), due=due_text, overdue_days=overdue_days,
            threshold_days=self.settings.escalation_threshold_days, nudge_day=nudged["day"],
            item_status=self.item_status(c.item_id) if c.item_id else None,
        )
        first_ever = not self.tracker.ever_escalated_about(c.member_id)
        return self._send(c, ESCALATION_PROPOSAL_TYPE, f"commitment_escalation:{c.id}", lead, content, first_ever, "escalation",
                          reason="an earlier escalation about this person was approved by a person")

    # --- the reminder -----------------------------------------------------------------------------------------------------------------

    def _nudge(self, c, due_text, days_to_due, events) -> FollowupResult:
        if NUDGED in events:
            return FollowupResult(c.id, c.member_id, "nudge", ALREADY_DONE, f"reminded on {events[NUDGED]['day']}")
        if not self.working:
            return FollowupResult(c.id, c.member_id, "nudge", NON_WORKING_DAY, "not a working day")
        if c.member_id in self.queued_today:
            return FollowupResult(c.id, c.member_id, "nudge", QUEUED, "one reminder per person per day: another is already going out")
        try:
            reading = self.cap.reading(c.member_id, self.today_iso)
        except SharedCapUnavailableError as exc:
            return FollowupResult(c.id, c.member_id, "nudge", CAP_UNAVAILABLE, f"nobody is reminded until it can be checked: {exc}")
        if reading.reached:
            return FollowupResult(c.id, c.member_id, "nudge", CAP_REACHED, reading.describe())
        content = messages.nudge_text(c, owner_name=self._name(c.member_id), due=due_text, days_to_due=days_to_due)
        first_ever = not self.cap.ever_nudged(c.member_id)
        return self._send(c, NUDGE_PROPOSAL_TYPE, f"commitment_nudge:{c.id}", c.member_id, content, first_ever, "nudge",
                          reason="this person has been reminded before, within the shared daily cap", ledger=True)

    # --- proposing, approving, sending ---------------------------------------------------------------------------------------------------

    def _send(self, c, proposal_type, key, recipient, content, first_ever, action, *, reason, ledger=False) -> FollowupResult:
        if self.dry_run:
            if action == "nudge":
                self.queued_today.add(c.member_id)
            return FollowupResult(c.id, c.member_id, action, WOULD_AWAIT if first_ever else WOULD_SEND,
                                  f"to {self._name(recipient) or recipient}" + (" (first one ever: needs a person's approval)" if first_ever else ""))
        proposal = self.store.get_by_idempotency_key(key)
        if proposal is None:
            payload = {
                "recipient_id": recipient, "recipient_name": self._name(recipient), "target_channel": recipient, "member_id": c.member_id,
                "commitment_id": c.id, "local_date": self.today_iso, "timezone": self.settings.timezone, "content": content,
                "delivery": "direct_message", "cap": self.settings.cap_per_person_per_day, "ledger_key": key,
            }
            refs = [f"commitment:{c.id}", *([f"message:{c.source_message_id}"] if c.source_message_id else []),
                    *([f"item:{c.item_id}"] if c.item_id else [])]
            proposal = self.store.create(type=proposal_type, payload=payload, original_model_output=payload, source_refs=refs, idempotency_key=key)
            service.write_audit(self.db_path, actor=service.AGENT, action="proposal.created", proposal_id=proposal.id,
                                details={"type": proposal_type, "recipient": recipient, "commitment": c.id})
            if ledger:
                self.cap.record(member_id=c.member_id, day=self.today_iso, commitment_id=c.id, proposal_id=proposal.id, key=key)
        if ledger:
            self.queued_today.add(c.member_id)

        if proposal.status == REJECTED:
            return FollowupResult(c.id, c.member_id, action, REJECTED_STATUS, "a person rejected it", proposal.id)
        if proposal.status == APPLIED:
            return FollowupResult(c.id, c.member_id, action, ALREADY_DONE, "already sent", proposal.id)
        if proposal.status == PENDING:
            if first_ever:
                return FollowupResult(c.id, c.member_id, action, AWAITING_APPROVAL, "the first one needs a person's approval", proposal.id)
            service.approve_automatically(proposal.id, reason=reason, store=self.store, db_path=self.db_path)
        if proposal.status in (PENDING, APPROVED):
            outcome = service.send_approved(proposal.id, publisher=self.publisher, policy=self.policy, store=self.store,
                                            db_path=self.db_path, now=self.moment)
            status = {service.SENT: SENT, service.SEND_FAILED: SEND_FAILED}.get(outcome.outcome, REFUSED)
            return FollowupResult(c.id, c.member_id, action, status, outcome.detail, proposal.id)
        return FollowupResult(c.id, c.member_id, action, REFUSED, f"proposal is {proposal.status}", proposal.id)
