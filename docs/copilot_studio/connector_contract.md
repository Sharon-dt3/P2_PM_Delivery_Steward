# Copilot Studio connector contract (PM-28)

The PM Delivery Steward reaches a person in Teams through three things, none of which is new code in the decision path:

| What the person sees | How it gets there |
|---|---|
| The morning brief and the end-of-day summary, in the project channel | The scheduled jobs PROPOSE them. Once a person approves, the message is posted through **P1's publish adapter** (`pm.adapters.teams.get_teams_publisher`, the same Power Automate flow P1 posts its digest with). Nothing is posted without an approval. |
| A proposal waiting for a decision | An **Adaptive Card** (`pm.approval.cards`) with Approve (and an edit box) and Reject, posted to the approver by a Power Automate flow. |
| The risk log, asked about in plain language | `/list_risks` and `/explain_risk`: the rows plus a sentence or two built from them by fixed templates. |

The API is `pm.api.copilot_studio_api` (`uv run uvicorn pm.api.copilot_studio_api:app`). `openapi.json` in this folder is generated from it
(`uv run python scripts/generate_openapi.py`); a test fails if the two differ. Import that file as a custom connector's definition.

## Authentication

| Header | On | Meaning |
|---|---|---|
| `X-API-Key` | every action endpoint | The shared secret `PM_COPILOT_API_KEY`. If the server has none, it refuses every action (HTTP 500). A wrong or missing key is HTTP 401. `/health` needs none. |
| `X-Authenticated-User` | `/card_action` | WHO pressed the button: the Teams responder's address, set **by the flow** from the card response, never by the card. Anything in the request body that names an approver is ignored. |

The approval service still checks that person against `PM_APPROVER_IDS`: anyone else is refused and the attempt is written to the audit log
under their name. So the header is only as trustworthy as the flow that sets it: **the flow must read the responder from the Teams action's
output and put it in this header; it must never copy it from the card data.** A per-user Entra check is the next step and needs a tenant.

## Endpoints

| Endpoint | Body | Returns |
|---|---|---|
| `GET /health` | none | status; whether a key and approvers are configured; whether a post would reach Teams or only a log (never a secret) |
| `POST /list_pending_approvals` | `{}` | `{"approvals": [{proposal_id, type, target_channel, local_date, created_at, summary, card}]}`, each with its Adaptive Card |
| `POST /card_action` | `{action: "approve"\|"reject", proposal_id, edited_content?, severity?, reason?}` | `{proposal_id, outcome, detail}`; `outcome` is `sent` (a message), `applied` (written to the risk log), `rejected`, `refused`, `send_failed` or `held` |
| `POST /get_decision_card` | `{proposal_id}` | `{"card": ...}`: who decided, when, what was proposed, what was applied (404 for an unknown proposal) |
| `POST /list_risks` | `{status?, severity?, related_item_id?}` | `{risks: [...], summary}` |
| `POST /explain_risk` | `{risk_id}` | `{risk, related_item, explanation}` (404 for an unknown risk) |

A decision always returns HTTP 200 with an `outcome` the flow branches on (a refused approval is a normal answer, not an error).

## What is the same on every surface

Approving in Teams, on the command line (`scripts/approve.py`) and in the dashboard all call `pm.approval.service.approve_and_send` or
`reject`. `tests/unit/test_copilot_api.py` decides the same pending proposal both ways, for approving as proposed, approving with edits,
rejecting with and without a reason, and an approval or rejection by someone who may not, and compares every row written: the audit events,
the send attempts and the proposal itself. They are equal.

Three kinds of card (every one is a decision by a person; nothing is applied without Approve). A **message** (brief, summary, reminder, escalation): an edit box, Approve, Reject. A **risk-log proposal** (a gap entry, or
the risk batch from P1's outcome record): a severity picker (`severity`, default medium), Approve (writes it to the risk log, outcome `applied`) and
Reject; no edit box. A **tracker batch**: no inputs, Approve (creates the items and adds the comments in the tracker, outcome `applied`) and Reject;
every item shows the channel message it came from and its whole line. The agent proposes no severity; the approver picks one, or `medium` is written and the audit says nobody chose.

## Not built (needs a tenant and a person in the maker portal)

The Copilot Studio agent, the custom connector registration, and the Power Automate flows (`power_automate_flows.md`). The API needs a URL
Microsoft's cloud can reach (a dev tunnel while testing, a hosted service for real use): that hosting decision is open.
