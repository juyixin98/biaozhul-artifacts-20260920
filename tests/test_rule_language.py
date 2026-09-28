"""Unit tests for the rule language: parsing, binding, validation."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

from htn_planner.models import Domain
from htn_planner.rule_language import (
    RuleLanguageError,
    bind_method,
    bind_primitive,
    load_domain,
    load_problem,
)

from .fixture_loader import DOMAIN_DIR, PROBLEM_DIR


def test_load_logistics_domain_parses_all_sections() -> None:
    domain = load_domain(DOMAIN_DIR / "logistics.yaml")
    assert domain.name == "logistics"
    assert domain.version == "1.0.0"
    assert "dock" in domain.resources
    assert domain.resources["dock"].capacity == 1
    assert {"move", "acquire_dock", "consume_token"} <= set(domain.primitives)
    task_names = {m.task for m in domain.methods}
    assert {"ship", "haul", "ship_two", "two_consume", "two_grab"} <= task_names


def test_primitive_parameter_binding_grounds_precondition_and_effect() -> None:
    domain = load_domain(DOMAIN_DIR / "logistics.yaml")
    ground = bind_primitive(domain.primitives["move"], ["truck", "depot", "city"])
    assert ground.precondition[0].literal_key() == ("at", "truck", "depot")
    assert ground.effect.remove[0] == ["at", "truck", "depot"]
    assert ground.effect.add[0] == ["at", "truck", "city"]
    # The template is not mutated.
    assert domain.primitives["move"].parameters == ["?carrier", "?from", "?to"]


def test_primitive_arity_mismatch_is_rejected() -> None:
    domain = load_domain(DOMAIN_DIR / "logistics.yaml")
    with pytest.raises(RuleLanguageError, match="arity mismatch"):
        bind_primitive(domain.primitives["move"], ["truck", "depot"])


def test_method_binding_rejects_unbound_variable() -> None:
    domain = load_domain(DOMAIN_DIR / "logistics.yaml")
    via = next(m for m in domain.methods if m.name == "m-haul-via-hub")
    # ?loc is derived only by the method's `bind` guard, so binding against
    # just the call args must fail unless the derived binding is supplied.
    with pytest.raises(RuleLanguageError, match="unbound variable"):
        bind_method(via, ["truck", "city"])
    grounded = bind_method(via, ["truck", "city"], extra_binding={"?loc": "depot"})
    assert grounded.subtasks[0].args == ["truck", "depot", "hub"]
    # An unrelated free variable is still rejected even with a derived scope.
    bad = via.model_copy(deep=True)
    bad.subtasks[0].args[1] = "?ghost"
    with pytest.raises(RuleLanguageError, match="unbound variable"):
        bind_method(bad, ["truck", "city"], extra_binding={"?loc": "depot"})


def test_problem_loads_recursive_budget_settings() -> None:
    problem = load_problem(PROBLEM_DIR / "assembly_depth_capped.yaml")
    assert problem.max_depth == 2
    assert problem.max_expansions == 256
    assert problem.goal_task == "assemble"


@pytest.mark.parametrize(
    "kwargs,match",
    [
        ({"subtasks": []}, "at least one subtask"),
    ],
)
def test_domain_model_rejects_empty_method_body(kwargs, match) -> None:
    from htn_planner.models import Method, Subtask

    with pytest.raises(Exception, match=match):
        Method(name="m", task="t", subtasks=kwargs["subtasks"])


def _minimal_domain(methods) -> Domain:
    from htn_planner.models import Primitive

    return Domain(
        name="d",
        primitives={"noop": Primitive(name="noop")},
        methods=methods,
    )


def test_duplicate_subtask_id_rejected() -> None:
    from htn_planner.models import Method, Subtask

    with pytest.raises(Exception, match="duplicate subtask id"):
        Method(
            name="m", task="t",
            subtasks=[
                Subtask(id="s", task="noop"),
                Subtask(id="s", task="noop"),
            ],
        )


def test_forward_order_reference_rejected_to_keep_dag_acyclic() -> None:
    from htn_planner.models import Method, Subtask

    with pytest.raises(Exception, match="earlier"):
        Method(
            name="m", task="t", order="partial",
            subtasks=[
                Subtask(id="a", task="noop", after=["b"]),
                Subtask(id="b", task="noop"),
            ],
        )


def test_unknown_sibling_dependency_rejected() -> None:
    from htn_planner.models import Method, Subtask

    with pytest.raises(Exception, match="unknown sibling"):
        Method(
            name="m", task="t", order="partial",
            subtasks=[Subtask(id="a", task="noop", after=["ghost"])],
        )


def test_sequential_body_cannot_declare_after_edges() -> None:
    from htn_planner.models import Method, Subtask

    domain = _minimal_domain([])
    from htn_planner.rule_language import _cross_validate

    method = Method(
        name="m", task="t",
        subtasks=[
            Subtask(id="a", task="noop"),
            Subtask(id="b", task="noop", after=["a"]),
        ],
    )
    # Model itself allows it (generic), cross-validation enforces order mode.
    domain.methods.append(method)
    with pytest.raises(RuleLanguageError, match="sequential subtask"):
        _cross_validate(domain)
