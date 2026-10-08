# Building the Power Automate flows (PM-28)

Built by hand in the Power Automate maker portal (tenant DigitalT3, default environment). The API they call is real and tested.
What exists and what was verified (2026-10-08):

| Piece | State |
|---|---|
| Custom connector "PM Delivery Steward v2" | built, validated, real calls reach the API |
| Flow "PM approvals in Teams" (manual trigger) | built and run once: card arrived in the approver's Teams chat (Workflows bot); **Approve** pressed in Teams called `/card_action` with `X-Authenticated-User` = the Teams responder's email; audit holds `proposal.approved` + `proposal.sent` with that approver |
| Recurrence trigger, "Get decision card" follow-up post | **not built**: the flow starts from a button; swap the trigger for a Recurrence to make it scheduled |
| Copilot Studio agent "PM Delivery Steward" | built (instructions + 4 read-only tools); **not tested**: the environment is out of Copilot credits (`EnforcementUsageCredits`), a tenant admin has to add credits; not published to any channel |

## Once: register the connector (done once for the demo: connector "PM Delivery Steward v2")
1. Put the API where Microsoft's cloud can reach it: `uv run python scripts/run_copilot_api.py --db data/pm_demo.db` (reads `.env`: a long random
   `PM_COPILOT_API_KEY`, `PM_APPROVER_IDS`, `TEAMS_PUBLISHER_MODE`; `scripts/prepare_teams_demo.py` builds the demo database) and a tunnel in front of it
   (`ngrok http 8765` was used). `GET /health` through the public address should answer.
2. Generate the file to import: `uv run python scripts/generate_openapi.py --connector https://YOUR-ADDRESS --skip-ngrok-warning`
   (writes `data/pm_connector_openapi.json`; leave out `--skip-ngrok-warning` if the address is not a free ngrok tunnel).
3. Power Automate, Custom connectors, New, **Import an OpenAPI file** (not "from URL": see below), choose that file, Continue, Create connector.
   The Security step is already API Key, header `X-API-Key`; the actions are named ListPendingApprovals, CardAction, GetDecisionCard, ListRisks, ExplainRisk, Health.
4. Test tab, New connection: paste the value of `PM_COPILOT_API_KEY`. Test `Health` and `ListPendingApprovals`: status 200, "Validation succeeded".

Things that cost time and are now handled:
- **Import from URL fails behind a free ngrok tunnel**, and so does every connector call: ngrok serves a browser warning page (HTML) to a browser-like
  caller, which is what Microsoft's importer and runtime look like. Import the FILE, and generate it with `--skip-ngrok-warning`, which makes every
  action send `ngrok-skip-browser-warning` as a hidden parameter. A tunnel without that page (a hosted address) does not need it.
- The importer insists that a hidden (internal) parameter with a default is marked required; the generator does.
- The importer is dependable with OpenAPI 3.0, so the generator converts the API's 3.1 document (optional fields become `nullable`).
- The API key is a connection setting: never put it in the file, a flow or the card.

## Flow A: ask for approvals ("PM approvals in Teams"; built with a manual button trigger, use a Recurrence to schedule it)
Fields as set in the new designer: `Action` = `data.action`, `Proposal Id` = `data.proposal_id`, `Edited Content` = `data.edited_content`
(from the card response), `X-authenticated-user` = `responder.email` (from the Teams action's own output). `Reason` is not mapped: the card does not collect one.
A risk-log proposal's card has a severity picker (`data.severity`), Approve and Reject; a tracker batch's card has no inputs, only Approve (writes the items and comments to the tracker) and Reject.
**Not yet mapped in the flow built on 2026-10-08:** it predates `severity`, so a Teams approval of a risk entry arrives with none and is written as
`medium`, recorded in the audit as a default. To carry the pick: regenerate and re-import the connector file (`scripts/generate_openapi.py --connector
URL --skip-ngrok-warning`), then set the new **Severity** field on "Approve or reject from a card" to `data.severity`.
1. **Recurrence** (as built: Manually trigger a flow).
2. **List pending approvals** (the connector). If the list is empty, stop.
3. **Apply to each** approval:
   1. Teams, **Post adaptive card and wait for a response**: recipient = the approver; card = `item('card')` (the connector returns it ready to use).
   2. **Card action** (the connector): `action`, `proposal_id`, `edited_content`, `reason` from the response data;
      **`X-Authenticated-User` = the responder's email from the Teams action's own output** (`responder/email`), not from the card.
   3. **Get decision card**, then Teams **Update adaptive card** / post it, so the approver sees who decided and what was applied.
4. Do not ask twice: a proposal already decided is no longer in the pending list.

## Flow B: nothing to build for delivery
The brief and the end-of-day summary are posted by the approval, through the Power Automate flow P1 already uses
(`POWER_AUTOMATE_FLOW_URL`). The same flow posts into any allowlisted channel from the `{action_type, target, content}` it is given.

## Copilot Studio agent
Built: agent "PM Delivery Steward" with `agent_instructions.md` as its instructions and four tools from the **v2** connector: List risks,
Explain a risk, List pending approvals, Get the decision card. Deliberately NOT given: Approve or reject from a card (a decision is a button
press on a card by an approver, never the agent) and Health check. The default "Search all websites" knowledge source was removed so the
agent can only say what a tool returned.

Not done: the test pane refuses every message ("This environment is out of credits", `EnforcementUsageCredits`), so the agent has not been
exercised. A tenant admin adds Copilot credits, then test the three questions in `agent_instructions.md` (open risks, tell me about RISK-002,
did the brief send) in the Preview tab before publishing to Teams. The Preview pane is hidden (display:none) in a narrow window; widen it.
