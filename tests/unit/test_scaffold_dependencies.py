"""Scaffold-only smoke test (PM-04's own DoD line: "P1's Teams reader
satisfies the chat dependency with no new code"). This does not exercise
any adapter at runtime -- that needs P1's config/fixture paths, which is
PM-04's own job to wire up -- it only proves the two cross-repo path
dependencies this whole plan rests on (p1, spine) actually resolve and
import under `uv run pytest`, the same way P1's own pythonpath bug
(fixed 2026-09-21, see ../P3_Agents/DECISION_LOG.md) was only caught by
an equivalent import-level check.
"""

from __future__ import annotations


def test_p1_package_imports() -> None:
    import p1  # noqa: F401


def test_spine_package_imports() -> None:
    import spine  # noqa: F401


def test_p1_adapters_factory_is_reachable() -> None:
    """The exact functions PM-04 will wire up: get_teams_reader() and
    get_teams_publisher(). Importing them here -- not calling them, since
    that needs P1's own config/fixture paths -- is what proves this
    repo's path dependency on p1 is enough to reach them with zero new
    adapter code."""
    from p1.adapters.factory import get_teams_reader, get_teams_publisher

    assert callable(get_teams_reader)
    assert callable(get_teams_publisher)


def test_p1_outcome_record_contract_is_reachable() -> None:
    """The CHN-26 contract PM-01/02's seed data will need to construct
    two OutcomeRecords against (one with allowlisted=False)."""
    from p1.contracts.outcome_record import OutcomeRecord, read_outcome, write_outcome

    assert callable(read_outcome)
    assert callable(write_outcome)
    assert "allowlisted" in OutcomeRecord.model_fields
