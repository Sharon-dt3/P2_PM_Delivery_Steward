"""Golden-case evaluation for the snapshot diff engine (PM-06/PM-07).

golden_cases.py holds hand-labeled ground truth, written by reading the
seeded transition log directly rather than by running compute_delta() and
copying its output -- the whole point is an independent check. runner.py
builds the real snapshots, runs the real diff engine, and scores its
output against those hand labels."""

from __future__ import annotations
