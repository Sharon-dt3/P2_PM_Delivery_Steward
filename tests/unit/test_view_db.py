"""The local database viewer: browse data/pm.db in your browser, read-only, on this
machine only. Nothing leaves the Mac, and it always shows the live file.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


@pytest.fixture()
def viewer():
    path = Path(__file__).parents[2] / "scripts" / "view_db.py"
    spec = importlib.util.spec_from_file_location("view_db_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_it_serves_read_only_and_only_on_this_machine(viewer, seeded_db_path):
    command = viewer.build_command(seeded_db_path, port=8081)

    assert "--read-only" in command or "-r" in command
    assert command[command.index("--host") + 1] == "127.0.0.1"  # never 0.0.0.0: not reachable from the network
    assert command[command.index("--port") + 1] == "8081"
    assert str(seeded_db_path) in command


def test_it_refuses_a_missing_database_and_does_not_create_one(viewer, tmp_path, capsys):
    code = viewer.main(["--db", str(tmp_path / "nope.db"), "--dry-run"])

    assert code == 1 and "not found" in capsys.readouterr().out
    assert not (tmp_path / "nope.db").exists()


def test_dry_run_prints_the_address_and_does_not_start_a_server(viewer, seeded_db_path, capsys):
    code = viewer.main(["--db", str(seeded_db_path), "--port", "8099", "--dry-run"])

    out = capsys.readouterr().out
    assert code == 0 and "http://127.0.0.1:8099" in out and "read-only" in out.lower()


def test_importing_the_script_starts_nothing(viewer):
    assert callable(viewer.main)
