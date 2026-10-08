-- 0006_risk_owner.sql
-- A risk-log entry can carry an owner: who has it, in words a person can read ("Olivia Dupree (olivia.dupree)").
--
-- Optional, and only ever set where it is evidenced: the owner a risk proposal suggested (the tracker's assignee of the item),
-- written when a person approves the proposal. Entries the lead writes by hand, and the three seeded ones, have none (NULL).

ALTER TABLE risks ADD COLUMN owner TEXT;
