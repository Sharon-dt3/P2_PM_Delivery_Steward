"""PM-28: the HTTP surface Copilot Studio and Power Automate call.

Three things reach a person on their phone before standup, and this is how each one gets there:

  The brief and the end-of-day summary are PROPOSED by the scheduled jobs and posted to the Teams channel, through P1's publish
  adapter (pm.adapters.teams.get_teams_publisher: the same Power Automate flow P1 already posts its digest with), only once a
  person approves them.

  Approvals are Adaptive Cards (pm.approval.cards). A Power Automate flow posts a card to the approver ("Post adaptive card and wait
  for a response"); when they press Approve or Reject, the flow calls /card_action with the card's data and WHO pressed it.

  The risk log is read conversationally: /list_risks and /explain_risk return the rows and a plain-language sentence or two,
  built from the rows by fixed templates (pm.api.risk_view), so the agent says what the log says and nothing it was not given.

This module decides nothing. Every handler is a thin call into code that already existed and is already tested
(pm.approval.cards, pm.approval.service), so a card pressed in Teams, the command line and the dashboard are the same function
and leave the same audit rows (tests/unit/test_copilot_api.py compares them row for row).

Authentication, in two parts, both fail-closed:

  X-API-Key             a shared secret (PM_COPILOT_API_KEY) that only the Power Platform connection and this server know. If it is
                        not set the API refuses every action endpoint (500), it never serves open. A wrong or missing key is a 401.
  X-Authenticated-User  WHO is acting: the Teams responder the flow read off the card response. It is never taken from the request
                        body (anything approver-like in the body is ignored), and the approval service still checks the person is
                        on the approver list (PM_APPROVER_IDS): an unknown person is refused and the attempt is audited.

The shared key is what stands between "anyone who can reach this URL" and "only Power Platform"; the header is only as trustworthy as
the flow that sets it, so the flow, not the card, must set it (docs/copilot_studio/connector_contract.md). A per-user Entra identity
check is the next step and needs a tenant.

Built by hand in the tenant's Power Platform maker portal, from this API's /openapi.json: the custom connector, the approvals flow (one
real Teams approval verified end to end) and the Copilot Studio agent (not exercised yet: the environment has no Copilot credits).
docs/copilot_studio/power_automate_flows.md says what exists and what does not.

Run: uv run uvicorn pm.api.copilot_studio_api:app   (never started by a test: tests drive it in process)
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Security
from fastapi.security import APIKeyHeader
from p1.adapters.teams_publisher_mock import LogPublisher
from pydantic import BaseModel, ConfigDict
from spine.approval.proposals import ProposalNotFoundError
from spine.storage.db import run_migrations

from pm.adapters.risk_log import RiskNotFoundError
from pm.adapters.teams import get_teams_publisher
from pm.adapters.tracker import ItemNotFoundError, TrackerMock
from pm.api import risk_view
from pm.approval import cards, service
from pm.approval.audit import audit_trail
from pm.risklog.csv_store import CsvRiskLog, live_risk_log_path
from pm.storage.db import DEFAULT_DB_PATH, MIGRATIONS_DIR

API_KEY_ENV_VAR = "PM_COPILOT_API_KEY"
DB_PATH_ENV_VAR = "PM_DB_PATH"


def _db_path() -> Path:
    """Read at request time, never frozen at import: a deployer changing .env and restarting, and a test pointing at its own database,
    both get the value they asked for."""
    return Path(os.environ.get(DB_PATH_ENV_VAR, str(DEFAULT_DB_PATH)))


@asynccontextmanager
async def _lifespan(app: FastAPI):
    run_migrations(_db_path(), MIGRATIONS_DIR)  # idempotent: a fresh deployment's database may have none applied yet
    yield


app = FastAPI(
    title="P2 PM Delivery Steward -- Copilot Studio connector backend",
    description=(
        "Proposal approvals as Adaptive Cards in Teams, and the risk log read conversationally. Import /openapi.json as a Power "
        "Platform custom connector's definition. Every action needs X-API-Key; the decision endpoints also need X-Authenticated-User, "
        "set by the flow from the Teams responder, never from the card."
    ),
    version="1.0.0",
    lifespan=_lifespan,
)


def get_publisher():
    """P1's publish adapter, chosen by TEAMS_PUBLISHER_MODE exactly as the scheduled jobs choose it (log-only unless set to power_automate)."""
    return get_teams_publisher()


def get_policy() -> service.ApprovalPolicy:
    return service.load_approval_policy()


_key_header = APIKeyHeader(
    name="X-API-Key", auto_error=False,
    description="The shared secret (PM_COPILOT_API_KEY) the Power Platform connection sends. Set it once in the connector's security tab.",
)


def require_api_key(x_api_key: Annotated[str | None, Security(_key_header)] = None) -> None:
    configured = os.environ.get(API_KEY_ENV_VAR)
    if not configured:
        raise HTTPException(status_code=500, detail=f"{API_KEY_ENV_VAR} is not set -- refusing to serve any action endpoint until it is.")
    if x_api_key != configured:
        raise HTTPException(status_code=401, detail="Missing or incorrect X-API-Key header.")


class CardAction(BaseModel):
    """What the card's Action.Submit posts back. Anything else in the body (an approver_id, say) is ignored."""

    model_config = ConfigDict(extra="ignore")

    action: str  # approve | reject
    proposal_id: str
    edited_content: str | None = None
    severity: str | None = None  # low | medium | high: only for a risk-log proposal; unrated means medium, recorded as a default
    reason: str | None = None


class ProposalRef(BaseModel):
    model_config = ConfigDict(extra="ignore")

    proposal_id: str


class RiskQuery(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: str | None = None  # open | mitigated | closed
    severity: str | None = None  # low | medium | high
    related_item_id: str | None = None


class RiskRef(BaseModel):
    model_config = ConfigDict(extra="ignore")

    risk_id: str


@app.get("/health", operation_id="Health", summary="Health check",
         description="No key needed. Says whether a key and approvers are configured (never what they are) and whether a post would reach Teams or only a log.")
def health() -> dict:
    """No key needed: a connectivity probe, never a capability. Says whether a key and approvers are configured (not what they are),
    and whether a post would reach Teams or only a log."""
    publisher = get_publisher()
    return {
        "status": "ok",
        "api_key_configured": bool(os.environ.get(API_KEY_ENV_VAR)),
        "approvers_configured": bool([a for a in os.environ.get("PM_APPROVER_IDS", "").split(",") if a.strip()]),
        "publisher": "log-only (nothing reaches Teams)" if isinstance(publisher, LogPublisher) else "power_automate (posts to Teams)",
        "database": _db_path().name,
    }


@app.post("/list_pending_approvals", operation_id="ListPendingApprovals", summary="List pending approvals",
          description="Every proposal awaiting a decision, each with the Adaptive Card to show for it (a morning brief or end-of-day summary to approve, or a batch from a channel record to read and reject).")
def list_pending_approvals(_: Annotated[None, Depends(require_api_key)]) -> dict:
    """Every proposal awaiting a decision, each with the Adaptive Card to show for it."""
    return cards.handle_list_pending({}, db_path=_db_path())


@app.post("/card_action", operation_id="CardAction", summary="Approve or reject from a card",
          description="The card's Approve or Reject. WHO is acting is the X-Authenticated-User header, set by the flow from the Teams responder, never from the card or the body. Always answers 200 with an outcome: sent (a message), applied (written to the risk log), rejected, refused, send_failed or held.")
def card_action(
    body: CardAction,
    publisher: Annotated[Any, Depends(get_publisher)],
    policy: Annotated[service.ApprovalPolicy, Depends(get_policy)],
    _: Annotated[None, Depends(require_api_key)],
    x_authenticated_user: Annotated[str | None, Header()] = None,
) -> dict:
    """The card's Approve or Reject. The approver is X-Authenticated-User, set by the flow from the Teams responder; the body cannot name one."""
    return cards.handle_card_action(
        body.model_dump(), authenticated_user_id=x_authenticated_user, publisher=publisher, policy=policy, db_path=_db_path(),
    )


@app.post("/get_decision_card", operation_id="GetDecisionCard", summary="Get the decision card",
          description="The card to show once a decision is made: who decided, when, what was proposed, what was applied.")
def get_decision_card(body: ProposalRef, _: Annotated[None, Depends(require_api_key)]) -> dict:
    """The card to show once a decision is made: who, when, what was proposed, what was applied."""
    try:
        return {"card": cards.decision_card(audit_trail(body.proposal_id, db_path=_db_path()))}
    except ProposalNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/list_risks", operation_id="ListRisks", summary="List risks",
          description="The risk log, optionally narrowed by status, severity or tracker item, with a plain-language summary. Reads only.")
def list_risks(body: RiskQuery, _: Annotated[None, Depends(require_api_key)]) -> dict:
    """The risk log, optionally narrowed by status, severity or tracker item, with a plain-language summary. Reads only."""
    risks = [
        r for r in CsvRiskLog(live_risk_log_path()).list_risks()
        if (not body.status or r.status == body.status) and (not body.severity or r.severity == body.severity)
        and (not body.related_item_id or r.related_item_id == body.related_item_id)
    ]
    return {
        "risks": [r.model_dump() for r in risks],
        "summary": risk_view.risk_summary(risks, filters={"status": body.status, "severity": body.severity, "item": body.related_item_id}),
    }


@app.post("/explain_risk", operation_id="ExplainRisk", summary="Explain a risk",
          description="One risk explained: its tracker item, who has it, and what is still waiting for a decision about it. Reads only.")
def explain_risk(body: RiskRef, _: Annotated[None, Depends(require_api_key)]) -> dict:
    """One risk explained: its item, who has it, and what is still waiting for a decision about it. Reads only."""
    try:
        risk = CsvRiskLog(live_risk_log_path()).get_risk(body.risk_id)
    except RiskNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    tracker = TrackerMock(db_path=_db_path())
    item = owner = None
    if risk.related_item_id:
        try:
            item = tracker.get_item(risk.related_item_id)
        except ItemNotFoundError:
            item = None
    if item is not None and item.assignee_id:
        owner = next((a.display_name for a in tracker.list_assignees() if a.id == item.assignee_id), item.assignee_id)
    waiting = [p.summary for p in service.list_pending_approvals(db_path=_db_path()) if risk.related_item_id and risk.related_item_id in p.summary]
    return {
        "risk": risk.model_dump(),
        "related_item": item.model_dump() if item is not None else None,
        "explanation": risk_view.explain_risk(risk, item, owner, waiting),
    }
