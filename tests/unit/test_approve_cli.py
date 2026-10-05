"""PM-13: the command-line surface -- the same service the card calls."""

from __future__ import annotations

import importlib.util
from datetime import datetime, time, timezone
from pathlib import Path

import pytest
from spine.approval.proposals import APPLIED, ProposalStore

from pm.eval.pm12_cases import ScriptedGateway
from pm.jobs.morning_brief_job import run_morning_brief_job
from pm.scheduling.config import ProjectScheduleConfig
from pm.seed.build import CHANNEL_ID

WED = datetime(2026, 9, 16, 2, 30, tzinfo=timezone.utc)


@pytest.fixture()
def cli():
    spec = importlib.util.spec_from_file_location("approve_script", Path(__file__).parents[2] / "scripts" / "approve.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def proposal(seeded_db_path):
    config = ProjectScheduleConfig(
        channel_id=CHANNEL_ID, timezone="Asia/Colombo", working_days=["Mon", "Tue", "Wed", "Thu", "Fri"],
        morning_brief_time=time(8, 0), end_of_day_time=time(17, 0),
    )
    return run_morning_brief_job(config, ScriptedGateway(), moment=WED, db_path=seeded_db_path).proposal_id


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("PM_APPROVER_IDS", "sharon.silva")
    monkeypatch.setenv("TEAMS_PUBLISHER_MODE", "mock")
    monkeypatch.setenv("TEAMS_PUBLISHER_LOG_PATH", str(tmp_path / "log.jsonl"))


def _run(cli, db, capsys, *argv):
    code = cli.main(["--db", str(db), *argv])
    return code, capsys.readouterr().out


def test_list_shows_the_pending_proposal(cli, seeded_db_path, proposal, capsys):
    code, out = _run(cli, seeded_db_path, capsys, "list")

    assert code == 0 and proposal in out and "2026-09-16" in out


def test_approve_then_audit_answers_the_question(cli, seeded_db_path, proposal, capsys):
    code, out = _run(cli, seeded_db_path, capsys, "approve", proposal, "--as", "sharon.silva", "--edit", "Edited via the CLI.")
    assert code == 0 and "sent" in out
    assert ProposalStore(seeded_db_path).get(proposal).status == APPLIED

    code, out = _run(cli, seeded_db_path, capsys, "audit", proposal)

    assert code == 0 and "sharon.silva" in out and "Edited via the CLI." in out and "Morning brief" in out


def test_an_unauthorised_approver_gets_a_non_zero_exit(cli, seeded_db_path, proposal, capsys):
    code, out = _run(cli, seeded_db_path, capsys, "approve", proposal, "--as", "mallory")

    assert code != 0 and "refused" in out.lower()
    assert ProposalStore(seeded_db_path).get(proposal).status == "pending"


def test_reject(cli, seeded_db_path, proposal, capsys):
    code, out = _run(cli, seeded_db_path, capsys, "reject", proposal, "--as", "sharon.silva", "--reason", "stale")

    assert code == 0 and "rejected" in out.lower()
