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
| `scripts/approve.py` (list / show / approve / reject / audit) | Real, tested |
| Adaptive Card JSON (`pm/approval/cards.py`) and its backend handler | Real, tested: the card's `Action.Submit` data is exactly the handler's request |
| The Copilot Studio agent that renders the card in Teams | Not built: needs a Microsoft tenant. Same status as P1's CHN-25 |

## Card contract

* `pending_brief_card(pending)` — shows the proposal; a text box (`edited_content`)
  pre-filled with it. **Approve** posts `{"action": "approve", "proposal_id": …,
  "edited_content": …}` (change the text first to approve with edits); **Reject**
  posts `{"action": "reject", "proposal_id": …}` and needs no input.
* `handle_card_action(request, authenticated_user_id=…)` →
  `{"proposal_id", "outcome": "sent" | "rejected" | "refused" | "send_failed", "detail"}`.
* `handle_list_pending({})` → `{"approvals": [{…, "card": <Adaptive Card>}]}`.
* `decision_card(trail)` — the card shown after a decision.
