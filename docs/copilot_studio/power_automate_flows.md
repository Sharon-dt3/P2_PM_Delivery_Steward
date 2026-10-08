# Building the Power Automate flows (PM-28)

Neither flow exists yet: they are built by hand in the Power Automate maker portal with tenant access. The API they call is real and tested.

## Once: register the connector
1. Run the API somewhere Microsoft can reach it (`PM_COPILOT_API_KEY`, `PM_APPROVER_IDS`, `TEAMS_PUBLISHER_MODE=power_automate` and
   `POWER_AUTOMATE_FLOW_URL` set; see `.env.example`). `GET /health` should answer, and say `power_automate (posts to Teams)`.
2. Power Apps / Power Automate, Custom connectors, New, Import an OpenAPI file: `docs/copilot_studio/openapi.json`. Set the host to your URL.
3. Security: API key, header name `X-API-Key`. Create a connection with the value of `PM_COPILOT_API_KEY`.

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
