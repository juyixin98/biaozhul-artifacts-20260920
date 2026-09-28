"""Boundary validation and YAML/JSON loading tests."""
from __future__ import annotations

import pytest

from app.rules.errors import FailureCategory, ValidationFailure
from app.rules.loader import load_file, loads


def test_fixtures_load_with_fixture_identity(drone_problem, workshop_problem, reactor_problem) -> None:
    assert drone_problem.name == "drone-delivery"
    assert workshop_problem.horizon == 6
    assert reactor_problem.actions[0].name == "run_pump"


@pytest.mark.semantics
def test_unknown_resource_reference_is_rejected() -> None:
    with pytest.raises(ValidationFailure) as excinfo:
        loads(
            """
            name: x
            horizon: 2
            goal: {fact: {fact: g, op: "==", value: 1}}
            actions:
              - name: a
                duration_min: 1
                resources: [ghost]
            """
        )
    assert excinfo.value.category == FailureCategory.INVALID_INPUT
    assert "ghost" in str(excinfo.value)


@pytest.mark.semantics
def test_duration_exceeding_horizon_is_rejected() -> None:
    with pytest.raises(ValidationFailure):
        loads(
            """
            name: x
            horizon: 1
            goal: {fact: {fact: g, op: "==", value: 1}}
            actions:
              - name: a
                duration_min: 2
            """
        )


@pytest.mark.semantics
def test_duplicate_action_names_rejected() -> None:
    with pytest.raises(ValidationFailure):
        loads(
            """
            name: x
            horizon: 2
            goal: {fact: {fact: g, op: "==", value: 1}}
            actions:
              - {name: a, duration_min: 1}
              - {name: a, duration_min: 1}
            """
        )


@pytest.mark.semantics
def test_bad_yaml_reports_source_and_is_invalid_input(tmp_path) -> None:
    path = tmp_path / "broken.yaml"
    path.write_text("name: : :\n  - bad", encoding="utf-8")
    with pytest.raises(ValidationFailure) as excinfo:
        load_file(path)
    assert excinfo.value.context.get("source") == str(path)


@pytest.mark.semantics
def test_json_loader_accepts_valid_definition() -> None:
    problem = loads(
        """
        {
          "name": "json-case",
          "horizon": 2,
          "initial": {"g": 1},
          "goal": {"fact": {"fact": "g", "op": "==", "value": 1}},
          "actions": [{"name": "idle", "duration_min": 1}]
        }
        """
    )
    assert problem.name == "json-case"


@pytest.mark.semantics
def test_missing_file_is_invalid_input_with_source(tmp_path) -> None:
    missing = tmp_path / "absent.yaml"
    with pytest.raises(ValidationFailure) as excinfo:
        load_file(missing)
    assert excinfo.value.context.get("source") == str(missing)


@pytest.mark.semantics
def test_malformed_json_file_reports_line_number(tmp_path) -> None:
    path = tmp_path / "broken.json"
    path.write_text('{"name": "x",\n broken', encoding="utf-8")
    with pytest.raises(ValidationFailure) as excinfo:
        load_file(path)
    assert excinfo.value.context.get("source") == str(path)


@pytest.mark.semantics
def test_non_mapping_document_is_invalid_input() -> None:
    with pytest.raises(ValidationFailure):
        loads("- just\n- a\n- list")


@pytest.mark.semantics
def test_unsupported_simultaneity_policy_rejected() -> None:
    with pytest.raises(ValidationFailure):
        loads(
            """
            name: x
            horizon: 2
            simultaneity_policy: SILENT_ARBITRARY_ORDER
            goal: {fact: {fact: g, op: "==", value: 1}}
            actions:
              - {name: a, duration_min: 1}
            """
        )
