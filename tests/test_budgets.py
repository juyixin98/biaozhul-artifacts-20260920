"""Budgets and numerical failure diagnostics."""

from __future__ import annotations

import pytest

from hvp_service.config import ServiceConfig
from hvp_service.errors import ErrorCategory, ServiceError
from hvp_service.service import HvpRequestData, HvpService

from .conftest import make_graph, quadratic_spec, unary_spec


def test_graph_node_budget_exceeded() -> None:
    service = HvpService(
        ServiceConfig(max_graph_nodes=5, max_eval_nodes=50_000, default_time_budget_ms=None)
    )
    nodes, output = quadratic_spec()  # 13 nodes > 5
    with pytest.raises(ServiceError) as exc:
        service.create_graph(nodes, output)
    err = exc.value
    assert err.category is ErrorCategory.RESOURCE_EXHAUSTED
    assert err.details["node_count"] == 13
    assert err.details["max_graph_nodes"] == 5


def test_eval_node_budget_exceeded(service, quad_point, quad_direction) -> None:
    nodes, output = quadratic_spec()
    gid = make_graph(service, nodes, output)
    with pytest.raises(ServiceError) as exc:
        service.hvp(
            gid,
            HvpRequestData(
                point=quad_point.tolist(),
                vector=quad_direction.tolist(),
                max_eval_nodes=10,  # combined program is much larger
            ),
        )
    err = exc.value
    assert err.category is ErrorCategory.RESOURCE_EXHAUSTED
    assert err.run_id
    assert err.details["program_nodes"] > 10
    assert err.details["max_eval_nodes"] == 10


def test_overflow_is_computation_failure_with_node_location(service) -> None:
    nodes, output = unary_spec("exp", n=1)
    gid = make_graph(service, nodes, output)
    with pytest.raises(ServiceError) as exc:
        service.hvp(gid, HvpRequestData(point=[800.0], vector=[1.0]))
    err = exc.value
    assert err.category is ErrorCategory.COMPUTATION_FAILURE
    assert err.run_id
    assert err.details["op"] == "exp"
    assert err.details["node_id"] == 1
    assert err.details["non_finite_count"] == 1


def test_domain_violation_is_computation_failure(service) -> None:
    nodes, output = unary_spec("log", n=1)
    gid = make_graph(service, nodes, output)
    with pytest.raises(ServiceError) as exc:
        service.hvp(gid, HvpRequestData(point=[-1.0], vector=[1.0]))
    err = exc.value
    assert err.category is ErrorCategory.COMPUTATION_FAILURE
    assert err.details["op"] == "log"


def test_time_budget_exceeded(service) -> None:
    # A chain graph large enough to cross the cooperative 64-node check
    # interval, with a near-zero time budget that must trip.
    nodes = [{"op": "input", "params": {"name": "x", "shape": [1]}}]
    for i in range(1, 200):
        nodes.append({"op": "tanh", "inputs": [i - 1]})
    nodes.append({"op": "sum", "inputs": [199]})
    gid = make_graph(service, nodes, 200)
    with pytest.raises(ServiceError) as exc:
        service.hvp(
            gid,
            HvpRequestData(point=[0.5], vector=[1.0], time_budget_ms=1e-9),
        )
    err = exc.value
    assert err.category is ErrorCategory.RESOURCE_EXHAUSTED
    assert err.run_id
    assert err.details["time_budget_ms"] == 1e-9
    assert err.details["nodes_evaluated"] > 0


def test_invalid_graph_specs_are_input_errors(service) -> None:
    # unknown op
    with pytest.raises(ServiceError) as exc:
        service.create_graph(
            [
                {"op": "input", "params": {"name": "x", "shape": [1]}},
                {"op": "frobnicate", "inputs": [0]},
                {"op": "sum", "inputs": [1]},
            ],
            2,
        )
    assert exc.value.category is ErrorCategory.INPUT_VALIDATION

    # forward reference (would be a cycle)
    with pytest.raises(ServiceError) as exc:
        service.create_graph(
            [
                {"op": "input", "params": {"name": "x", "shape": [1]}},
                {"op": "add", "inputs": [0, 2]},
            ],
            1,
        )
    assert exc.value.category is ErrorCategory.INPUT_VALIDATION

    # non-scalar output
    with pytest.raises(ServiceError) as exc:
        service.create_graph(
            [
                {"op": "input", "params": {"name": "x", "shape": [2]}},
                {"op": "exp", "inputs": [0]},
            ],
            1,
        )
    assert exc.value.category is ErrorCategory.INPUT_VALIDATION
