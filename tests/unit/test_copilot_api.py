"""PM-28: the HTTP surface Copilot Studio and Power Automate call: proposal approvals as Adaptive Cards in Teams, the brief and the
end-of-day summary delivered through P1's publish adapter, and the risk log read conversationally.

What is real and tested here: the HTTP API (auth that fails closed, the identity taken from the platform and never from the request,
the card JSON, the OpenAPI document a custom connector imports) and, above all, that a decision made from Teams leaves exactly the
audit record the command line and the dashboard leave. What needs a tenant and a person in the Power Platform maker portal, and is
not built here: the Copilot Studio agent and the Power Automate flows themselves (docs/copilot_studio/ says how to make them).
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
from datetime import datetime, time, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from p1.adapters.teams_publisher_mock import LogPublisher
from spine.approval.proposals import APPLIED, PENDING, REJECTED, ProposalStore

from pm.api import copilot_studio_api as api
from pm.approval import service
from pm.approval.audit import audit_trail
from pm.approval.service import ApprovalPolicy
from pm.eval.pm12_cases import ScriptedGateway
from pm.jobs.end_of_day_job import run_end_of_day_job
from pm.jobs.morning_brief_job import run_morning_brief_job
from pm.jobs.snapshot_capture import capture_snapshot
from pm.reporting.scripted_summary import ScriptedSummaryGateway
from pm.scheduling.config import ProjectScheduleConfig
from pm.seed.build import CHANNEL_ID

KEY = "test-key"
WED = datetime(2026, 9, 16, 2, 30, tzinfo=timezone.utc)  # 08:00 in Colombo
EVENING = datetime(2026, 9, 16, 11, 30, tzinfo=timezone.utc)  # 17:00 in Colombo
POLICY = ApprovalPolicy(approver_ids=frozenset({"sharon.silva"}), allowlisted_channel_ids=[])


def _config() -> ProjectScheduleConfig:
    return ProjectScheduleConfig(
        channel_id=CHANNEL_ID, timezone="Asia/Colombo", working_days=["Mon", "Tue", "Wed", "Thu", "Fri"],
        morning_brief_time=time(8, 0), end_of_day_time=time(17, 0),
    )


@pytest.fixture()
def brief_id(seeded_db_path) -> str:
    return run_morning_brief_job(_config(), ScriptedGateway(), moment=WED, db_path=seeded_db_path).proposal_id


@pytest.fixture()
def teams(seeded_db_path, tmp_path, monkeypatch):
    """The API running against the seeded database with a log-only publisher, as Power Automate would call it."""
    monkeypatch.setenv("PM_DB_PATH", str(seeded_db_path))
    monkeypatch.setenv("PM_COPILOT_API_KEY", KEY)
    monkeypatch.setenv("PM_APPROVER_IDS", "sharon.silva")
    log = LogPublisher(tmp_path / "teams_out.jsonl")
    api.app.dependency_overrides[api.get_publisher] = lambda: log
    with TestClient(api.app) as client:
        yield SimpleNamespace(client=client, log=log, db=seeded_db_path)
    api.app.dependency_overrides.clear()


def headers(user: str | None = "sharon.silva", key: str | None = KEY) -> dict:
    out = {}
    if key is not None:
        out["X-API-Key"] = key
    if user is not None:
        out["X-Authenticated-User"] = user
    return out


def act(teams, body: dict, **kw):
    return teams.client.post("/card_action", json=body, headers=headers(**kw))


# --- the door: a key that fails closed -----------------------------------------------------------------------------------------------


def test_health_needs_no_key_and_says_what_is_configured_without_revealing_it(teams):
    body = teams.client.get("/health").json()

    assert body["status"] == "ok" and body["api_key_configured"] is True and KEY not in json.dumps(body)


@pytest.mark.parametrize("path", ["/list_pending_approvals", "/card_action", "/get_decision_card", "/list_risks", "/explain_risk"])
def test_every_action_endpoint_refuses_a_missing_or_wrong_key(teams, path):
    assert teams.client.post(path, json={}, headers=headers(key=None)).status_code == 401
    assert teams.client.post(path, json={}, headers=headers(key="wrong")).status_code == 401


def test_with_no_key_configured_it_refuses_everything_rather_than_serving_open(teams, monkeypatch):
    monkeypatch.delenv("PM_COPILOT_API_KEY")

    response = teams.client.post("/list_pending_approvals", json={}, headers=headers())

    assert response.status_code == 500 and "PM_COPILOT_API_KEY" in response.json()["detail"]


# --- cards in Teams ---------------------------------------------------------------------------------------------------------------------


def test_the_pending_list_gives_each_proposal_its_adaptive_card(teams, brief_id):
    body = teams.client.post("/list_pending_approvals", json={}, headers=headers()).json()

    (approval,) = body["approvals"]
    assert approval["proposal_id"] == brief_id and approval["card"]["type"] == "AdaptiveCard" and approval["card"]["version"] == "1.5"
    assert {a["title"] for a in approval["card"]["actions"]} == {"Approve", "Reject"}


def test_the_decision_card_shows_who_decided_and_what_was_applied(teams, brief_id):
    act(teams, {"action": "approve", "proposal_id": brief_id})

    card = teams.client.post("/get_decision_card", json={"proposal_id": brief_id}, headers=headers()).json()["card"]

    text = json.dumps(card)
    assert "sharon.silva" in text and "Applied as proposed" in text


def test_a_decision_card_for_an_unknown_proposal_is_a_404(teams):
    assert teams.client.post("/get_decision_card", json={"proposal_id": "nope"}, headers=headers()).status_code == 404


# --- deciding from Teams ----------------------------------------------------------------------------------------------------------------


def test_approving_from_teams_posts_the_brief_through_the_publisher(teams, brief_id):
    content = ProposalStore(teams.db).get(brief_id).payload["content"]

    result = act(teams, {"action": "approve", "proposal_id": brief_id}).json()

    assert result["outcome"] == service.SENT and result["proposal_id"] == brief_id
    (posted,) = teams.log.read_log()
    assert posted["action_type"] == "channel_post" and posted["target"] == CHANNEL_ID and posted["content"] == content
    assert ProposalStore(teams.db).get(brief_id).status == APPLIED


def test_approving_with_edits_posts_the_edited_text_and_keeps_the_original(teams, brief_id):
    result = act(teams, {"action": "approve", "proposal_id": brief_id, "edited_content": "Edited in Teams."}).json()

    assert result["outcome"] == service.SENT
    assert teams.log.read_log()[0]["content"] == "Edited in Teams."
    trail = audit_trail(brief_id, db_path=teams.db)
    assert trail.edited and trail.final_proposal["content"] == "Edited in Teams." and trail.original_proposal["content"] != "Edited in Teams."


def test_rejecting_from_teams_posts_nothing_and_records_why(teams, brief_id):
    result = act(teams, {"action": "reject", "proposal_id": brief_id, "reason": "wrong sprint"}).json()

    assert result["outcome"] == service.REJECTED_OUTCOME
    assert teams.log.read_log() == [] and ProposalStore(teams.db).get(brief_id).status == REJECTED
    assert any(e["details"].get("reason") == "wrong sprint" for e in audit_trail(brief_id, db_path=teams.db).events)


def test_who_is_acting_comes_from_the_platform_never_from_the_request(teams, brief_id):
    """A request that names sharon.silva as the approver, sent as someone else, is the someone else: and they may not approve."""
    result = act(teams, {"action": "approve", "proposal_id": brief_id, "approver_id": "sharon.silva", "approver": "sharon.silva"},
                 user="mallory@example.com").json()

    assert result["outcome"] == service.REFUSED and teams.log.read_log() == []
    denied = [e for e in audit_trail(brief_id, db_path=teams.db).events if e["action"] == "proposal.denied"]
    assert [e["actor"] for e in denied] == ["mallory@example.com"]


def test_no_authenticated_user_means_no_decision(teams, brief_id):
    result = act(teams, {"action": "approve", "proposal_id": brief_id}, user=None).json()

    assert result["outcome"] == service.REFUSED and "no authenticated user" in result["detail"]
    assert ProposalStore(teams.db).get(brief_id).status == PENDING


@pytest.mark.parametrize("body", [{"proposal_id": "x"}, {"action": "delete", "proposal_id": "x"}, {"action": "approve"}])
def test_a_malformed_card_submission_is_refused_without_touching_anything(teams, brief_id, body):
    response = act(teams, body)

    assert response.status_code in (200, 422)
    if response.status_code == 200:
        assert response.json()["outcome"] == service.REFUSED
    assert ProposalStore(teams.db).get(brief_id).status == PENDING


def test_deciding_twice_is_refused_the_second_time_and_posts_once(teams, brief_id):
    first = act(teams, {"action": "approve", "proposal_id": brief_id}).json()
    second = act(teams, {"action": "approve", "proposal_id": brief_id}).json()

    assert first["outcome"] == service.SENT and second["outcome"] != service.SENT
    assert len(teams.log.read_log()) == 1


def test_a_publisher_that_fails_leaves_the_proposal_to_retry_and_says_so(teams, brief_id):
    class Down(LogPublisher):
        def post_channel_message(self, channel_id, content):
            raise RuntimeError("flow unreachable")

    api.app.dependency_overrides[api.get_publisher] = lambda: Down(teams.log._log_path)

    result = act(teams, {"action": "approve", "proposal_id": brief_id}).json()

    assert result["outcome"] == service.SEND_FAILED and "flow unreachable" in result["detail"]
    assert ProposalStore(teams.db).get(brief_id).status != APPLIED


# --- the brief and the end-of-day summary, delivered ------------------------------------------------------------------------------------


def test_the_end_of_day_summary_is_delivered_the_same_way(teams, seeded_db_path):
    capture_snapshot(_config(), moment=WED, db_path=seeded_db_path)
    run = run_end_of_day_job(_config(), ScriptedSummaryGateway(), moment=EVENING, db_path=seeded_db_path)
    assert run.proposal_id

    (approval,) = [a for a in teams.client.post("/list_pending_approvals", json={}, headers=headers()).json()["approvals"]
                   if a["proposal_id"] == run.proposal_id]
    result = act(teams, {"action": "approve", "proposal_id": run.proposal_id}).json()

    assert approval["type"] == "end_of_day_summary_publish" and "End-of-day summary" in approval["summary"]
    assert result["outcome"] == service.SENT
    assert "End-of-day summary" in teams.log.read_log()[0]["content"]


# --- the acceptance test: the same audit record as the fallback surface ------------------------------------------------------------------


_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def _scrub(value):
    """Everything about a decision's record except WHEN it was written."""
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items() if k not in ("created_at", "decided_at", "at", "sent_at", "logged_at")}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return _TIME.sub("<time>", value) if isinstance(value, str) else value


def _record(db, proposal_id):
    """Every row the gate wrote about this proposal: the audit events, the send attempts, and the proposal itself."""
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        audit = [dict(r) for r in conn.execute(
            "SELECT actor, action, entity_type, entity_id, details FROM audit WHERE entity_id = ? ORDER BY id", (proposal_id,))]
        writes = [dict(r) for r in conn.execute(
            "SELECT action_type, target, status, payload FROM write_log WHERE proposal_id = ? ORDER BY id", (proposal_id,))]
        proposal = dict(conn.execute("SELECT type, status, approver_id, payload, original_model_output FROM proposals WHERE id = ?", (proposal_id,)).fetchone())
    finally:
        conn.close()
    for row in audit:
        row["details"] = json.loads(row["details"])
    for row in writes:
        row["payload"] = json.loads(row["payload"])
    return _scrub({"audit": audit, "write_log": writes, "proposal": proposal})


DECISIONS = [
    ("approved as proposed", {"action": "approve"}, "sharon.silva"),
    ("approved with edits", {"action": "approve", "edited_content": "Edited before sending."}, "sharon.silva"),
    ("rejected with a reason", {"action": "reject", "reason": "wrong sprint"}, "sharon.silva"),
    ("rejected without a reason", {"action": "reject"}, "sharon.silva"),
    ("approval by someone who may not", {"action": "approve"}, "mallory@example.com"),
    ("rejection by someone who may not", {"action": "reject"}, "mallory@example.com"),
]


@pytest.mark.parametrize("label, request_body, user", DECISIONS, ids=[d[0] for d in DECISIONS])
def test_a_decision_made_from_teams_leaves_the_same_audit_record_as_the_command_line(teams, brief_id, tmp_path, label, request_body, user):
    """Two copies of one pending proposal. One is decided the way the command line and the dashboard decide
    (pm.approval.service directly), the other by a card submitted over HTTP. Every row written must be the same."""
    fallback_db = tmp_path / "fallback.db"
    shutil.copy(teams.db, fallback_db)
    fallback_log = LogPublisher(tmp_path / "fallback_out.jsonl")

    if request_body["action"] == "approve":
        service.approve_and_send(brief_id, approver_id=user, edited_content=request_body.get("edited_content"),
                                 publisher=fallback_log, policy=POLICY, db_path=fallback_db)
    else:
        service.reject(brief_id, approver_id=user, reason=request_body.get("reason"), policy=POLICY, db_path=fallback_db)
    act(teams, {**request_body, "proposal_id": brief_id}, user=user)

    from_teams, from_the_command_line = _record(teams.db, brief_id), _record(fallback_db, brief_id)
    assert from_teams == from_the_command_line
    assert from_teams["audit"], "a decision must leave an audit record at all"
    assert [r["content"] for r in teams.log.read_log()] == [r["content"] for r in fallback_log.read_log()]


def test_the_trail_a_person_reads_back_is_the_same_too(teams, brief_id, tmp_path):
    fallback_db = tmp_path / "fallback.db"
    shutil.copy(teams.db, fallback_db)
    service.approve_and_send(brief_id, approver_id="sharon.silva", edited_content="x", publisher=LogPublisher(tmp_path / "o.jsonl"),
                             policy=POLICY, db_path=fallback_db)
    act(teams, {"action": "approve", "proposal_id": brief_id, "edited_content": "x"})

    def trail(db):
        t = audit_trail(brief_id, db_path=db)
        return _scrub({"status": t.status, "approver": t.approver_id, "edited": t.edited, "original": t.original_proposal,
                       "final": t.final_proposal, "sent": t.sent, "events": t.events})

    assert trail(teams.db) == trail(fallback_db)


# --- the risk log, read conversationally ------------------------------------------------------------------------------------------------


def test_the_risk_log_is_listed_with_a_plain_language_summary(teams):
    body = teams.client.post("/list_risks", json={}, headers=headers()).json()

    assert [r["id"] for r in body["risks"]] == ["RISK-001", "RISK-002", "RISK-003"]
    assert "3 risks" in body["summary"] and "RISK-002" in body["summary"] and "PM-024" in body["summary"]


def test_the_risk_log_can_be_asked_about_by_status_severity_and_item(teams):
    def ask(**kw):
        return [r["id"] for r in teams.client.post("/list_risks", json=kw, headers=headers()).json()["risks"]]

    assert ask(status="open") == ["RISK-001", "RISK-002"] and ask(status="mitigated") == ["RISK-003"]
    assert ask(related_item_id="PM-024") == ["RISK-002"]
    assert set(ask(severity="high")) <= {"RISK-001", "RISK-002", "RISK-003"} and ask(severity="high") != []


def test_a_question_the_log_cannot_answer_says_so(teams):
    body = teams.client.post("/list_risks", json={"related_item_id": "PM-999"}, headers=headers()).json()

    assert body["risks"] == [] and "no risks" in body["summary"].lower()


def test_one_risk_is_explained_with_its_item_its_owner_and_what_is_waiting(teams):
    body = teams.client.post("/explain_risk", json={"risk_id": "RISK-002"}, headers=headers()).json()

    text = body["explanation"]
    assert "RISK-002" in text and "PM-024" in text and "Olivia Dupont" in text and "blocked" in text
    assert body["risk"]["id"] == "RISK-002" and body["related_item"]["id"] == "PM-024"


def test_an_unknown_risk_is_a_404(teams):
    assert teams.client.post("/explain_risk", json={"risk_id": "RISK-999"}, headers=headers()).status_code == 404


def test_reading_the_risk_log_changes_nothing(teams):
    before = _all_counts(teams.db)

    teams.client.post("/list_risks", json={}, headers=headers())
    teams.client.post("/explain_risk", json={"risk_id": "RISK-001"}, headers=headers())

    assert _all_counts(teams.db) == before


def _all_counts(db):
    conn = sqlite3.connect(db)
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("proposals", "audit", "write_log", "risks", "items")}


# --- through P1's real publish adapter, with the network mocked ----------------------------------------------------------------------------


def _power_automate(requests: list):
    """P1's own PowerAutomateTeamsPublisher, its HTTP client pointed at a stand-in for the flow."""
    import httpx
    from p1.adapters.teams_publisher_power_automate import PowerAutomateTeamsPublisher

    def flow(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    return PowerAutomateTeamsPublisher("https://flow.example.invalid/hook", client=httpx.Client(transport=httpx.MockTransport(flow)))


def test_approving_in_teams_sends_the_brief_through_p1s_power_automate_publisher(teams, brief_id):
    sent: list[dict] = []
    api.app.dependency_overrides[api.get_publisher] = lambda: _power_automate(sent)
    api.app.dependency_overrides[api.get_policy] = lambda: ApprovalPolicy(approver_ids=frozenset({"sharon.silva"}), allowlisted_channel_ids=[CHANNEL_ID])
    content = ProposalStore(teams.db).get(brief_id).payload["content"]

    result = act(teams, {"action": "approve", "proposal_id": brief_id}).json()

    assert result["outcome"] == service.SENT
    assert sent == [{"action_type": "channel_post", "target": CHANNEL_ID, "content": content}]  # exactly what the flow is given


def test_a_real_publisher_still_cannot_post_to_a_channel_that_is_not_on_the_allowlist(teams, brief_id):
    sent: list[dict] = []
    api.app.dependency_overrides[api.get_publisher] = lambda: _power_automate(sent)
    api.app.dependency_overrides[api.get_policy] = lambda: ApprovalPolicy(approver_ids=frozenset({"sharon.silva"}), allowlisted_channel_ids=[])

    result = act(teams, {"action": "approve", "proposal_id": brief_id}).json()

    assert result["outcome"] == service.REFUSED and "allowlist" in result["detail"]
    assert sent == [] and ProposalStore(teams.db).get(brief_id).status == PENDING


# --- the document a custom connector imports ----------------------------------------------------------------------------------------------


def test_the_published_openapi_document_is_the_one_the_api_serves():
    from pathlib import Path

    published = Path(__file__).resolve().parents[2] / "docs" / "copilot_studio" / "openapi.json"

    assert json.loads(published.read_text()) == api.app.openapi(), "regenerate it: uv run python scripts/generate_openapi.py"


def test_every_action_in_the_document_asks_for_the_key_and_the_decision_asks_for_the_user():
    spec = api.app.openapi()
    for path, methods in spec["paths"].items():
        if path == "/health":
            continue
        params = {p["name"].lower() for p in methods["post"].get("parameters", [])}
        assert "x-api-key" in params, path
    assert "x-authenticated-user" in {p["name"].lower() for p in spec["paths"]["/card_action"]["post"]["parameters"]}
