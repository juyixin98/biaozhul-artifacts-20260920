"""Configuration tests: defaults, environment overrides, validation."""

from __future__ import annotations

import pytest

from config.settings import (
    BudgetConfig,
    load_budget,
    load_log,
    load_settings,
)


def test_default_budget_values():
    budget = BudgetConfig()
    assert budget.max_degree == 64
    assert budget.max_coefficient_bits == 4096
    assert budget.max_sturm_pairs == 2_000_000
    assert budget.max_bisection_depth == 200
    assert budget.max_roots == 256


def test_environment_overrides_budget(monkeypatch):
    monkeypatch.setenv("RI_MAX_DEGREE", "12")
    monkeypatch.setenv("RI_MAX_ROOTS", "7")
    budget = load_budget()
    assert budget.max_degree == 12
    assert budget.max_roots == 7


def test_invalid_degree_environment_raises(monkeypatch):
    monkeypatch.setenv("RI_MAX_DEGREE", "not-an-int")
    with pytest.raises(ValueError, match="integer"):
        load_budget()


def test_nonpositive_budget_rejected(monkeypatch):
    monkeypatch.setenv("RI_MAX_DEGREE", "0")
    with pytest.raises(ValueError, match="positive"):
        load_budget()


def test_log_redaction_defaults_on(monkeypatch):
    monkeypatch.delenv("RI_LOG_REDACT", raising=False)
    assert load_log().redact_polynomials is True


def test_log_redaction_can_be_disabled(monkeypatch):
    monkeypatch.setenv("RI_LOG_REDACT", "false")
    assert load_log().redact_polynomials is False


def test_full_settings_load(monkeypatch):
    monkeypatch.setenv("RI_PORT", "9999")
    monkeypatch.setenv("RI_HOST", "127.0.0.1")
    settings = load_settings()
    assert settings.port == 9999
    assert settings.host == "127.0.0.1"
    assert settings.budget.max_degree == 64
