# The approval gate (PM-13)

Nothing the agent writes reaches Teams except through an approved proposal.

```
morning job ──proposes──▶ proposal (pending) ──▶ person decides ──▶ service executes ──▶ Teams adapter
                                                   │  approve | reject | edit-then-approve
                                                   ▼
                                  audit (who, when) · write_log (every send attempt)
```

* **Spine reuse.** `spine.approval.ProposalStore` holds the proposal and its
  status machine (pending → approved → applied, or rejected);
  `spine.approval.guarded_send` refuses to call the adapter for anything not
  approved. P2 adds `pm/approval/`: the brief as a proposal, the service, the
  audit trail, the cards.
* **Enforcement is in the service** (`pm/approval/service.py`), never in a card or
  a script: only listed approvers (`PM_APPROVER_IDS`; none listed means nobody),
  a refused attempt is itself audited, an edit replaces what is sent but never
  what the agent originally proposed, and a real publisher may only post to a
  channel on P1's allowlist.
* **Who is acting** is supplied by the platform (Copilot Studio's authenticated
  user), never read from card data.
* **The audit trail** (`pm/approval/audit.py`, `scripts/approve.py audit ID`)
  answers: who approved this entry, when, and what did the agent originally
  propose — plus what was finally applied and every send attempt.

## Unattended mode: auto-approve

`PM_AUTO_APPROVE=1` lets the system approve a brief itself, so nobody has to
click. It goes through the same gate (proposal, approval, `guarded_send`, logs,
audit); only the approver differs. The approval is recorded as
`system:auto-approve` with `automatic: true` and the reason, never as a person,
and no person can act under that id. It happens only when:

* grounding dropped nothing and no fact fell back to "[as recorded]" -- anything
  else waits for a person;
* a person has already approved a brief for that channel (the first post to a
  channel is never unattended), unless `PM_AUTO_APPROVE_REQUIRES_FIRST_HUMAN=0`;
* a real publisher's target is on the channel allowlist.

The text is never edited. A failed send leaves it approved; `approve.py retry ID`
sends it. Off unless the switch is exactly `1`.

## Surfaces

| Surface | Status |
|---|---|
| `app/approval_dashboard.py` (Streamlit): P2 approvals plus a read-only P1 tab | Real, tested headlessly with Streamlit's AppTest. Run: `uv run streamlit run app/approval_dashboard.py` |
| `scripts/approve.py` (list / show / approve / reject / retry / audit) | Real, tested |
| Adaptive Card JSON (`pm/approval/cards.py`) and its backend handler | Real, tested: the card's `Action.Submit` data is exactly the handler's request |
| The Copilot Studio agent that renders the card in Teams | Not built: needs a Microsoft tenant and credits, so the Streamlit dashboard is the working human surface, as in P1 |

## Card contract

* `pending_brief_card(pending)` — shows the proposal; a text box (`edited_content`)
  pre-filled with it. **Approve** posts `{"action": "approve", "proposal_id": …,
  "edited_content": …}` (change the text first to approve with edits); **Reject**
  posts `{"action": "reject", "proposal_id": …}` and needs no input.
* `handle_card_action(request, authenticated_user_id=…)` →
  `{"proposal_id", "outcome": "sent" | "rejected" | "refused" | "send_failed", "detail"}`.
* `handle_list_pending({})` → `{"approvals": [{…, "card": <Adaptive Card>}]}`.
* `decision_card(trail)` — the card shown after a decision.

## The combined Streamlit dashboard

One page, one sign-in, a tab per project: **P2 morning brief** and **P1 channel**.
Run it with `uv run streamlit run app/approval_dashboard.py`. A header shows how
many items are waiting in each project.

* **P2 tab:** pending briefs with the exact text to be posted, why each is still
  waiting (from `explain_hold`), an edit box, Approve and Reject, retry for an
  approved brief whose send failed, and the audit trail. Thin on purpose: every
  button calls `pm.approval.service`, so it can do nothing the CLI and card cannot.
* **P1 tab: read-only.** It lists what P1 has waiting, through P1's own approval
  service and database (`P1_DB_PATH`, default `../P3_Agents/data/p1_live.db`), and
  has no button: who may approve in P1 is decided in P1's repo. Approve P1 items in
  P1's own dashboard. Looking never writes to P1's database.
* **Known weakness:** P1's service has no approver list of its own, so P2 cannot
  enforce one for P1; that would be a change in P1's repo.
* **Who is acting:** Streamlit has no sign-in of its own, so you choose from
  `PM_APPROVER_IDS` (the service still refuses anyone not on it; `PM_DASHBOARD_USER`
  preselects one). The audit trail is only as trustworthy as that choice: for
  production, put the page behind company single sign-on. The sidebar shows what is
  switched on (publisher, auto-approve, approvers) and never the flow URL.

## Golden case 6: approval enforcement (PM-14)

`scripts/run_eval.py` prints two hard-zero metrics, and both must pass:

* **GC6-write-bypass-count.** 21 direct write attempts, each on its own fresh
  proposal: 11 against a PENDING one (the guard called directly, the service's send,
  approving without being an approver, a blank approver, the system's own id, the
  card handler with no user or a forged approver, poking the store, internals) and
  10 against a REJECTED one (re-approval, edit-then-approve, `store.approve`, a
  payload rewrite after the decision, auto-approve, the card handler). An attempt
  counts if it reaches the adapter, posts a line, leaves a "sent" row, or changes
  the proposal's status, payload, approver or decision time. Each must also end in a
  recognised refusal, so a crash in the probe never counts as a block.
* **GC6-audit-gap-count.** For four decided proposals (approved with edits, approved
  as proposed, approved automatically, rejected) the raw rows must show the approver,
  the timestamp, the original payload (never overwritten by an edit) and the applied
  payload (matching what the write log recorded and what the adapter was handed), and
  the application's own audit trail must agree with the raw rows.

`tests/unit/test_eval_pm14.py` breaks each safeguard in turn and checks that GC6
notices.
