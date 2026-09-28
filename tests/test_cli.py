"""Tests for the command-line interface."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from htn_planner.cli import run

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
DOMAINS = FIXTURES / "domains"
PROBLEMS = FIXTURES / "problems"


class TestCli:
    def test_success_outputs_json_with_verification(self, tmp_path, capsys) -> None:
        code = run(
            [
                "--domain", str(DOMAINS / "logistics.htn"),
                "--problem", str(PROBLEMS / "logistics_direct.pddl"),
                "--request-id", "cli-1",
            ]
        )
        assert code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["request_id"] == "cli-1"
        assert payload["result"]["status"] == "success"
        assert payload["verification"]["ok"] is True

    def test_failure_case_exit_zero_and_category_present(self, capsys) -> None:
        # Planning failures are normal results (exit 0), encoded in JSON.
        code = run(
            [
                "--domain", str(DOMAINS / "logistics.htn"),
                "--problem", str(PROBLEMS / "logistics_unreachable.pddl"),
                "--request-id", "cli-fail",
            ]
        )
        assert code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["result"]["status"] == "failure"
        assert payload["result"]["failures"][0]["category"] == "no_applicable_method"

    def test_inconclusive_case(self, capsys) -> None:
        code = run(
            [
                "--domain", str(DOMAINS / "bounds.htn"),
                "--problem", str(PROBLEMS / "bounds_walk_long.pddl"),
                "--max-depth", "1",
                "--compact",
            ]
        )
        assert code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["result"]["status"] == "inconclusive"

    def test_parse_error_exit_two(self, tmp_path, capsys) -> None:
        bad = tmp_path / "bad.htn"
        bad.write_text("(:domain broken")
        prob = tmp_path / "p.pddl"
        prob.write_text("(:problem p (:domain d) (:init) (:tasks (!a)))")
        code = run(["--domain", str(bad), "--problem", str(prob)])
        assert code == 2
        err = json.loads(capsys.readouterr().err)
        assert err["error"] == "invalid_input"

    def test_db_persistence_flag(self, tmp_path, capsys) -> None:
        db = tmp_path / "cli.db"
        code = run(
            [
                "--domain", str(DOMAINS / "docks.htn"),
                "--problem", str(PROBLEMS / "docks_two.pddl"),
                "--db", str(db),
                "--request-id", "cli-db",
            ]
        )
        assert code == 0
        assert db.exists()
