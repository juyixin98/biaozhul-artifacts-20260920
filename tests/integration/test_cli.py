"""Integration tests for the reproducibility CLI."""
from __future__ import annotations

import json

import pytest

from ssp.cli import main


@pytest.mark.integration
def test_fixtures_command_replays_all_and_exits_zero(capsys):
    rc = main(["fixtures", "--trials", "800"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "6 OK, 0 flagged" in captured.out
    for name in [
        "normal_textbook.json",
        "normal_t_small.json",
        "normal_two_sample_d20.json",
        "binomial_low_rate_exact.json",
        "binomial_moderate.json",
        "binomial_fisher.json",
    ]:
        assert name in captured.out


@pytest.mark.integration
def test_normal_command_prints_committed_plan(capsys):
    rc = main([
        "normal", "--alternative", "two_sided", "--alpha", "0.05",
        "--power", "0.8", "--effect", "0.5", "--scale", "standardized_d",
        "--known-sigma", "--trials", "800",
    ])
    assert rc == 0
    body = json.loads(capsys.readouterr().out)
    plan = body["plan"]
    assert plan["allocation"]["n0"] == 32
    assert plan["minimal_integer_check"]["passes"] is True
    assert body["evidence"]["trials"] == 800


@pytest.mark.integration
def test_failed_plan_returns_nonzero_with_category(capsys):
    # Zero effect is a validation failure: exit code 2, explicit category.
    rc = main([
        "normal", "--alternative", "two_sided", "--alpha", "0.05",
        "--power", "0.8", "--effect", "0.0", "--scale", "standardized_d",
    ])
    assert rc == 2
    err = json.loads(capsys.readouterr().err)
    assert err["category"] == "validation_error"


@pytest.mark.integration
def test_binomial_exact_command(capsys):
    rc = main([
        "binomial", "--alternative", "greater", "--alpha", "0.05",
        "--power", "0.8", "--p0", "0.01", "--effect", "0.03",
        "--scale", "proportions", "--trials", "800",
    ])
    assert rc == 0
    body = json.loads(capsys.readouterr().out)
    assert body["plan"]["method"] == "binomial_exact_one_sample"
    assert body["plan"]["allocation"]["n0"] == 301
