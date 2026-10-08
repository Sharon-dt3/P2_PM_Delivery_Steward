# Building the Power Automate flows (PM-28)

Neither flow exists yet: they are built by hand in the Power Automate maker portal with tenant access. The API they call is real and tested.

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

## Flow A: ask for approvals (a recurrence, every few minutes in working hours)
1. **Recurrence.**
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
Add the connector's actions as tools. Paste `agent_instructions.md` as the agent's instructions. Test in the Copilot Studio test pane with a
tracked person's identity before publishing to Teams.
