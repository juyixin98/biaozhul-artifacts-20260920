"""Tests for the command-line entry point (normal and rejected runs)."""

from __future__ import annotations

import json

from ipwate.cli import main


def test_cli_good_overlap_emits_evidence_json(capsys):
    rc = main(["--scenario", "good_overlap", "--n", "800", "--seed", "42",
               "--request-id", "cli-ok", "--show-true-ate"])
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload["request_id"] == "cli-ok"
    assert payload["verdict"] in ("accept", "warn")
    assert payload["contract"]["estimand"] == "ate"
    assert abs(payload["estimate"]["point"] - payload["ground_truth"]["ate_true_on_sample"]) < 0.6


def test_cli_no_overlap_returns_structured_error(capsys):
    rc = main(["--scenario", "no_overlap", "--n", "800", "--seed", "9"])
    out = capsys.readouterr().out
    assert rc == 2
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "positivity_violation"


def test_cli_att_with_clipping(capsys):
    rc = main(["--scenario", "poor_overlap", "--n", "1000", "--seed", "11",
               "--estimand", "att", "--clipping"])
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert rc == 0
    assert payload["contract"]["estimand"] == "att"
    assert payload["contract"]["clipping_enabled"] is True
