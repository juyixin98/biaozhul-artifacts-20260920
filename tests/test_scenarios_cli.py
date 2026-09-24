"""Acceptance tests: every built-in scenario replays and meets expectations,
the cryptographic chain verifies, and the CLI behaves correctly."""

import json
import subprocess
import sys

import pytest

from time_alignment.scenario import (
    GENERATORS,
    check_expectations,
    format_evidence,
    run_scenario,
    write_example_scenarios,
)
from time_alignment.cli import main as cli_main
from time_alignment.storage import ChainVerifyError, Storage

SCENARIO_NAMES = sorted(GENERATORS)


@pytest.mark.parametrize("name", SCENARIO_NAMES)
def test_scenario_expectations(name):
    scenario = GENERATORS[name]()
    summary = run_scenario(scenario)
    failures = check_expectations(summary, scenario)
    assert not failures, f"{name} expectation failures:\n" + "\n".join(
        "  - " + f for f in failures)
    assert summary["chain"]["ok"] is True


def test_all_required_edge_cases_covered():
    names = set(SCENARIO_NAMES)
    assert "burst" in names          # sudden traffic burst
    assert "drops" in names          # packet loss
    assert "identical_timestamps" in names
    assert "clock_reset" in names    # clock reset / new epoch
    assert "out_of_order" in names   # late arrivals


def test_evidence_contains_tie_and_reason():
    scenario = GENERATORS["identical_timestamps"]()
    summary = run_scenario(scenario)
    tied = [d for d in summary["decisions"] if d["tie"]]
    assert len(tied) >= 2
    assert all(d["reason"].startswith("tie_") for d in tied)
    table = format_evidence(summary)
    assert "MATCHED" in table and "tie" in table.lower()


def test_write_and_reload_example_files(tmp_path):
    paths = write_example_scenarios(str(tmp_path / "examples"))
    assert len(paths) == len(SCENARIO_NAMES)
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            scenario = json.load(fh)
        assert {"name", "config", "events", "expect"} <= set(scenario)
        summary = run_scenario(scenario)
        assert not check_expectations(summary, scenario)


def test_cli_replay_success(tmp_path, capsys):
    scenario = GENERATORS["burst"]()
    path = tmp_path / "burst.json"
    path.write_text(json.dumps(scenario), encoding="utf-8")
    rc = cli_main(["replay", str(path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "EXPECTATIONS: PASS" in out
    assert "chain" in out.lower()


def test_cli_replay_to_db_and_verify(tmp_path, capsys):
    scenario = GENERATORS["clock_reset"]()
    sj = tmp_path / "reset.json"
    sj.write_text(json.dumps(scenario), encoding="utf-8")
    db = str(tmp_path / "out.sqlite")
    rc = cli_main(["replay", str(sj), "--db", db])
    assert rc == 0
    assert cli_main(["verify", db]) == 0
    out = capsys.readouterr().out
    assert "CHAIN OK" in out


def test_cli_verify_detects_tamper(tmp_path, capsys):
    scenario = GENERATORS["burst"]()
    db = str(tmp_path / "t.sqlite")
    run_scenario(scenario, db_path=db)
    import sqlite3
    conn = sqlite3.connect(db)
    conn.execute("UPDATE decisions SET imu_seq = 9999 WHERE seq = 1")
    conn.commit()
    conn.close()
    rc = cli_main(["verify", db])
    assert rc == 2
    assert "TAMPERED" in capsys.readouterr().out


def test_cli_report_file(tmp_path):
    scenario = GENERATORS["drops"]()
    sj = tmp_path / "d.json"
    sj.write_text(json.dumps(scenario), encoding="utf-8")
    report = tmp_path / "report.json"
    rc = cli_main(["replay", str(sj), "--report", str(report)])
    assert rc == 0
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["chain"]["ok"] is True
    assert data["status_counts"]["CAMERA_UNMATCHED"] >= 1
    assert data["decisions"]
