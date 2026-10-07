# The shared per-person per-day nudge cap

Two agents each politely chasing the same person is how an agent estate becomes a nuisance. P1 caps its own nudges
(`nudge_cap_per_day`, per person per **channel** per day). That cap cannot see P2, and P2's cannot see P1's, so each could
be inside its own limit while one person is chased twice in a day.

The shared cap counts a person's **delivered** nudges for a day across both agents, with one number:

- **P1's ledger:** the `nudges` table in P1's database (`P1_DB_PATH`, default P1's `data/p1_live.db`): every channel, rows with
  a `sent_at`. P2 opens it **read-only** and never writes to it.
- **P2's ledger:** the `nudges` table in P2's own database, the same shape (`member_id`, `date`, `sent_at`).
- A pending or rejected nudge was never delivered, so it never counts.
- **Checked twice:** when a reminder is planned, and again at the moment it is sent (a first reminder waits for approval
  and can be approved after P1 already chased the person that day).
- **Fails closed:** if P1's ledger is configured but cannot be read, nobody is reminded. Only when P1 has never run here
  (no `P1_DB_PATH` set and no ledger at the default location) is P1's count zero, and the reading says so.
- **One source of truth:** P1's ledger location is found from the environment for planning AND for sending.
- **One person, several ids.** P1's live ledger names people by their Microsoft Graph user id; P2's roster uses its own ids.
  A P2 person's count includes every id they appear under in P1: their own, and those of P1 `members` with the same display
  name (exact, ignoring case and spacing). Two P1 members with one name are both counted: not knowing which is which, the
  safe answer is to count both. If P1 has no member of that name, only an exact id can match.
- The cap is `PM_NUDGE_CAP_PER_PERSON_PER_DAY`, else P1's channel `nudge_cap_per_day` (default 1).
- A "day" is the project's local date. For a P1 channel in another timezone a nudge near midnight can land on the
  neighbouring day.
- An escalation to the lead is not a reminder to the lead and is not counted.

## The other direction (done in P1)

P1's nudge job now reads this agent's ledger too. It is opt-in and off by default: with two environment settings in P1's
`.env`, restarted live runners count a person's delivered nudges across every P1 channel plus this agent's `nudges` table
(read-only; people matched across systems by display name) and refuse at the cap, before a proposal exists and again on every
rerun. It fails closed if this agent's database cannot be read. See P1's `DECISION_LOG.md` (2026-10-07) and
`src/p1/nudges/shared_cap.py`.

```
P1_SHARED_NUDGE_CAP_PER_DAY=1                      # the same number as PM_NUDGE_CAP_PER_PERSON_PER_DAY
P1_PEER_NUDGE_LEDGERS=/path/to/P2_PM_Delivery_Steward/data/pm.db
```

`tests/unit/test_cross_agent_nudge_cap.py` runs P1's real nudge job and this agent's real follow-up against each other's
databases, in both orders, for one person who is `wei.chen` here and a Graph user id in P1: whichever reminds her first, the
other holds back; and a control run without P1's side shows she would otherwise be chased twice.
