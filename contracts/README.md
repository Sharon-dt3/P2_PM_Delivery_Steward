# Published contracts this agent consumes

`outcome_record.v1.schema.json` is P1's published channel outcome record schema (CHN-26), copied byte for byte from
`P3_Agents/schema/outcome_record.v1.schema.json`. P2 reads a record with this file and nothing else of P1's: no import of
anything in `p1.*`. `tests/unit/test_channel_cross_agent.py` fails if this copy and P1's published schema ever differ, and
proves the reader works with `p1` made un-importable.

To take a new schema version: copy the new file in, keep the old one while records of the old version still exist.
