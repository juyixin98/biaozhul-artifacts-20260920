"""Boundary/error-path tests for the condition engine and configuration."""
from __future__ import annotations

import importlib
import os

import pytest

from app.rules.conditions import (
    apply_effects,
    evaluate,
    normalize_state,
    referenced_facts,
)
from app.rules.errors import FailureCategory, ValidationFailure


@pytest.mark.semantics
@pytest.mark.parametrize(
    "condition",
    [
        "not-a-dict",
        {"a": 1, "b": 2},
        {"all": "not-a-list"},
        {"any": {"x": 1}},
        {"not": ["x"]},
        {"fact": "not-a-dict"},
        {"fact": {"op": "==", "value": 1}},
        {"fact": {"fact": "f", "op": "~", "value": 1}},
        {"fact": {"fact": "f"}},
        {"bogus": 1},
    ],
)
def test_evaluate_rejects_malformed_conditions(condition) -> None:
    with pytest.raises(ValidationFailure) as excinfo:
        evaluate(condition, {})
    assert excinfo.value.category == FailureCategory.INVALID_INPUT


@pytest.mark.semantics
def test_evaluate_order_comparison_rejects_bool_and_bad_value() -> None:
    with pytest.raises(ValidationFailure):
        evaluate({"fact": {"fact": "f", "op": ">=", "value": True}}, {"f": 1})
    with pytest.raises(ValidationFailure):
        evaluate({"fact": {"fact": "f", "op": "<", "value": "x"}}, {"f": 1})


@pytest.mark.semantics
@pytest.mark.parametrize(
    "effects,state",
    [
        ("not-a-list", {}),
        (["not-a-dict"], {}),
        ([{"op": "=", "value": 1}], {}),
        ([{"fact": "f", "op": "??", "value": 1}], {}),
        ([{"fact": "f"}], {}),
        ([{"fact": "f", "op": "=", "value": 1.5}], {}),
        ([{"fact": "f", "op": "+=", "value": "x"}], {}),
        ([{"fact": "f", "op": "+=", "value": 1}], {"f": True}),
    ],
)
def test_apply_effects_rejects_malformed_inputs(effects, state) -> None:
    with pytest.raises(ValidationFailure):
        apply_effects(effects, state)


@pytest.mark.semantics
def test_normalize_state_validates_keys_and_values() -> None:
    with pytest.raises(ValidationFailure):
        normalize_state([("a", 1)], field="initial")
    with pytest.raises(ValidationFailure):
        normalize_state({"": 1}, field="initial")
    with pytest.raises(ValidationFailure):
        normalize_state({"f": 1.5}, field="initial")
    assert normalize_state({"f": 1, "b": True}, field="initial") == {"f": 1, "b": True}
    assert normalize_state(None, field="initial") == {}


@pytest.mark.semantics
def test_referenced_facts_rejects_malformed() -> None:
    with pytest.raises(ValidationFailure):
        referenced_facts("x")
    with pytest.raises(ValidationFailure):
        referenced_facts({"unknown": 1})


def test_settings_support_explicit_overrides(tmp_path) -> None:
    import app.config as config

    settings = config.load_settings({
        "db_path": str(tmp_path / "x.db"),
        "log_level": "DEBUG",
        "default_budget_nodes": 1234,
        "default_max_steps": 7,
    })
    assert settings.db_path == str(tmp_path / "x.db")
    assert settings.log_level == "DEBUG"
    assert settings.default_budget_nodes == 1234
    assert settings.default_max_steps == 7
    assert settings.version
    assert (tmp_path / "x.db").parent.exists()


def test_settings_read_environment_and_validate(monkeypatch, tmp_path) -> None:
    import app.config as config

    monkeypatch.setenv("PLANNER_DB_PATH", str(tmp_path / "env.db"))
    monkeypatch.setenv("PLANNER_DEFAULT_BUDGET", "42")
    monkeypatch.setenv("PLANNER_LOG_LEVEL", "warning")
    settings = config.load_settings()
    assert settings.default_budget_nodes == 42
    assert settings.log_level == "WARNING"

    monkeypatch.setenv("PLANNER_DEFAULT_BUDGET", "not-an-int")
    with pytest.raises(ValueError):
        config.load_settings()

    monkeypatch.delenv("PLANNER_DEFAULT_BUDGET")
    monkeypatch.setenv("PLANNER_LOG_LEVEL", "BOGUS")
    with pytest.raises(ValueError):
        config.load_settings()


def test_runtime_versions_reports_core_stack() -> None:
    from app.config import runtime_versions

    versions = runtime_versions()
    assert versions["service"]
    assert versions["python"].startswith("3.")
    assert versions["fastapi"]
    assert versions["pydantic"]
