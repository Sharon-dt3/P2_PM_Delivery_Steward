"""scripts/risk_log.py: show the risk log, compare the three stores, and sync them."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from pm.adapters.risk_log import RiskLogStore
from pm.risklog.csv_store import DEFAULT_CSV_PATH, CsvRiskLog, read_risks, write_risks
from pm.risklog.remote import RemoteUnavailableError

SEEDED = read_risks(DEFAULT_CSV_PATH)
SECRET_URL = "postgresql://postgres:s3cr3t-pass@db.example.invalid:5432/postgres"


class Remote(RiskLogStore):
    def __init__(self, risks=(), down=False):
        self.rows = {r.id: r for r in risks}
        self.down = down

    def _up(self):
        if self.down:
            raise RemoteUnavailableError("could not reach the lead-facing store")

    def list_risks(self):
        self._up()
        return sorted(self.rows.values(), key=lambda r: r.id)

    def replace_all(self, risks):
        self._up()
        self.rows = {r.id: r for r in risks}

    def get_risk(self, risk_id):
        return self.rows[risk_id]

    def create_risk(self, payload):
        self.rows[payload.id] = payload
        return payload

    def update_risk(self, risk_id, payload):
        self.rows[risk_id] = payload
        return payload


@pytest.fixture()
def cli():
    path = Path(__file__).parents[2] / "scripts" / "risk_log.py"
    spec = importlib.util.spec_from_file_location("risk_log_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def args(tmp_path, seeded_db_path):
    csv_path = tmp_path / "risks.csv"
    write_risks(csv_path, SEEDED)
    return ["--db", str(seeded_db_path), "--csv", str(csv_path), "--baseline", str(tmp_path / "baseline.json")], csv_path


def _run(cli, capsys, argv, remote):
    code = cli.main(argv, remote=remote)
    return code, capsys.readouterr().out


def test_show_lists_the_log_readably(cli, args, capsys):
    common, _ = args

    code, out = _run(cli, capsys, [*common, "show"], Remote())

    assert code == 0
    for text in ("RISK-001", "RISK-002", "RISK-003", "high", "mitigated", "PM-023", "Billing sync nightly job"):
        assert text in out


def test_status_is_clean_only_when_all_three_stores_match(cli, args, capsys):
    common, _ = args

    code, out = _run(cli, capsys, [*common, "status"], Remote())
    assert code == 1 and "lead store" in out and "0" in out  # the lead's table is still empty: not in sync

    assert _run(cli, capsys, [*common, "sync"], Remote(SEEDED))[0] == 0  # identical content: agreed
    remote = Remote(SEEDED)
    code, out = _run(cli, capsys, [*common, "status"], remote)
    assert code == 0 and "in sync" in out.lower() and "3" in out


def test_sync_pushes_the_repo_log_to_the_lead_store_the_first_time(cli, args, capsys):
    common, _ = args
    remote = Remote()

    code, out = _run(cli, capsys, [*common, "sync"], remote)

    assert code == 0 and "pushed" in out.lower() and list(remote.rows) == ["RISK-001", "RISK-002", "RISK-003"]


def test_a_lead_edit_is_pulled_and_a_dry_run_shows_it_without_applying(cli, args, capsys):
    common, csv_path = args
    remote = Remote()
    _run(cli, capsys, [*common, "sync"], remote)
    remote.rows["RISK-001"] = remote.rows["RISK-001"].model_copy(update={"status": "mitigated"})

    code, out = _run(cli, capsys, [*common, "sync", "--dry-run"], remote)
    assert code == 0 and "RISK-001" in out and "dry run" in out.lower()
    assert read_risks(csv_path)[0].status == "open"  # nothing applied

    code, out = _run(cli, capsys, [*common, "sync"], remote)
    assert code == 0 and "pulled" in out.lower() and read_risks(csv_path)[0].status == "mitigated"


def test_a_conflict_exits_non_zero_and_names_what_differs(cli, args, capsys):
    common, csv_path = args
    remote = Remote()
    _run(cli, capsys, [*common, "sync"], remote)
    CsvRiskLog(csv_path).update_risk("RISK-001", read_risks(csv_path)[0].model_copy(update={"severity": "high"}))
    remote.rows["RISK-002"] = remote.rows["RISK-002"].model_copy(update={"status": "closed"})

    code, out = _run(cli, capsys, [*common, "sync"], remote)

    assert code == 1 and "conflict" in out.lower() and "RISK-001" in out and "RISK-002" in out


def test_push_and_pull_force_a_direction(cli, args, capsys):
    common, csv_path = args
    remote = Remote()
    _run(cli, capsys, [*common, "sync"], remote)
    remote.rows["RISK-003"] = remote.rows["RISK-003"].model_copy(update={"status": "closed"})
    CsvRiskLog(csv_path).update_risk("RISK-001", read_risks(csv_path)[0].model_copy(update={"severity": "high"}))

    assert _run(cli, capsys, [*common, "pull"], remote)[0] == 0
    assert read_risks(csv_path)[2].status == "closed" and read_risks(csv_path)[0].severity == "medium"

    CsvRiskLog(csv_path).update_risk("RISK-001", read_risks(csv_path)[0].model_copy(update={"severity": "high"}))
    assert _run(cli, capsys, [*common, "push"], remote)[0] == 0
    assert remote.rows["RISK-001"].severity == "high"


def test_an_unreachable_lead_store_is_reported_and_exits_non_zero(cli, args, capsys):
    common, _ = args

    code, out = _run(cli, capsys, [*common, "sync"], Remote(SEEDED, down=True))

    assert code == 1 and "could not be reached" in out


def test_invalid_edits_in_the_lead_store_are_refused_with_every_problem(cli, args, capsys):
    common, csv_path = args
    remote = Remote()
    _run(cli, capsys, [*common, "sync"], remote)
    remote.rows["RISK-001"] = remote.rows["RISK-001"].model_copy(update={"severity": "urgent", "status": "done"})

    code, out = _run(cli, capsys, [*common, "sync"], remote)

    assert code == 1 and "urgent" in out and "done" in out
    assert read_risks(csv_path)[0].severity == "medium"


def test_without_a_lead_store_configured_it_says_so_and_never_prints_a_url(cli, args, capsys, monkeypatch):
    common, _ = args
    monkeypatch.setenv("SUPABASE_DB_URL", "")

    code = cli.main([*common, "sync"])
    out = capsys.readouterr().out

    assert code == 1 and "SUPABASE_DB_URL" in out


def test_the_connection_string_is_never_printed(cli, args, capsys, monkeypatch):
    common, _ = args
    monkeypatch.setenv("SUPABASE_DB_URL", SECRET_URL)

    class Boom(Remote):
        def list_risks(self):
            raise RemoteUnavailableError(f"could not connect to {SECRET_URL}")

    code, out = _run(cli, capsys, [*common, "sync"], Boom())

    assert code == 1 and "s3cr3t-pass" not in out
