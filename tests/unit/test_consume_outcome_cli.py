"""scripts/consume_outcome.py: the demo entry point for PM-26."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CHANNEL = "19:proj-gamma@thread.tacv2"


def _script():
    spec = importlib.util.spec_from_file_location("consume_outcome_script", REPO / "scripts" / "consume_outcome.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _record(tmp_path, **overrides) -> Path:
    data = {
        "schema_version": "1.0", "channel_id": CHANNEL, "channel_display_name": "Project Gamma", "date": "2026-09-18", "allowlisted": True,
        "roster": [], "generated_at": "2026-09-18T11:30:00+00:00", "participation": [], "updates": [], "decisions": [], "questions": [],
        "blockers": [{"message_id": "m4", "text": "PM-014 is still blocked on the staging migration.", "quote": None}],
    }
    data.update(overrides)
    path = tmp_path / "2026-09-18.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _proposals(db) -> int:
    return sqlite3.connect(db).execute("SELECT COUNT(*) FROM proposals").fetchone()[0]


def test_a_dry_run_shows_the_plan_and_creates_nothing(tmp_path, seeded_db_path, capsys):
    code = _script().main([str(_record(tmp_path)), "--db", str(seeded_db_path), "--dry-run"])

    out = capsys.readouterr().out
    assert code == 0 and "tracker changes (1)" in out and "risk-log entries (1)" in out and "comment on PM-014" in out
    assert "Project Gamma message m4" in out and "would propose 1 item(s)" in out
    assert _proposals(seeded_db_path) == 0


def test_without_a_dry_run_it_proposes_both_batches_and_says_so(tmp_path, seeded_db_path, capsys):
    code = _script().main([str(_record(tmp_path)), "--db", str(seeded_db_path)])

    out = capsys.readouterr().out
    assert code == 0 and "tracker batch: proposed 1 item(s)" in out and "risk batch: proposed 1 item(s)" in out
    assert _proposals(seeded_db_path) == 2


def test_a_record_that_is_not_cleared_is_refused_with_a_non_zero_exit(tmp_path, seeded_db_path, capsys):
    code = _script().main([str(_record(tmp_path, allowlisted=False)), "--db", str(seeded_db_path)])

    out = capsys.readouterr().out
    assert code == 2 and "REFUSED (not_allowlisted)" in out and "zero proposals" in out
    assert _proposals(seeded_db_path) == 0


def test_the_record_is_found_from_the_channel_and_the_date_by_the_published_layout(tmp_path, seeded_db_path, monkeypatch, capsys):
    folder = tmp_path / "outcomes" / "19_proj-gamma_thread.tacv2"  # the contract's slug of the channel id
    folder.mkdir(parents=True)
    _record(folder)  # writes <folder>/2026-09-18.json
    monkeypatch.setenv("P1_OUTCOMES_DIR", str(tmp_path / "outcomes"))

    code = _script().main(["--channel-id", CHANNEL, "--date", "2026-09-18", "--db", str(seeded_db_path), "--dry-run"])

    assert code == 0 and "Project Gamma 2026-09-18" in capsys.readouterr().out
