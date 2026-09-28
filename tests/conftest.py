"""Shared fixtures: graph specs and a service instance per test."""

from __future__ import annotations

import numpy as np
import pytest

from hvp_service.config import ServiceConfig
from hvp_service.service import HvpService

from .references import QUAD_A, QUAD_B


@pytest.fixture()
def service() -> HvpService:
    return HvpService(
        ServiceConfig(max_graph_nodes=2_000, max_eval_nodes=50_000, default_time_budget_ms=None)
    )


def make_graph(service: HvpService, nodes: list[dict], output: int) -> str:
    graph_id, _ = service.create_graph(nodes, output)
    return graph_id


# --- graph specs ------------------------------------------------------------


def quadratic_spec() -> tuple[list[dict], int]:
    """f(x) = 0.5 x^T A x + b^T x for x in R^3."""
    nodes = [
        {"op": "input", "params": {"name": "x", "shape": [3]}},  # 0
        {"op": "const", "params": {"value": QUAD_A.tolist()}},  # 1
        {"op": "reshape", "inputs": [0], "params": {"shape": [3, 1]}},  # 2
        {"op": "matmul", "inputs": [1, 2]},  # 3
        {"op": "reshape", "inputs": [3], "params": {"shape": [3]}},  # 4
        {"op": "mul", "inputs": [0, 4]},  # 5
        {"op": "sum", "inputs": [5]},  # 6
        {"op": "const", "params": {"value": 0.5}},  # 7
        {"op": "mul", "inputs": [6, 7]},  # 8
        {"op": "const", "params": {"value": QUAD_B.tolist()}},  # 9
        {"op": "mul", "inputs": [0, 9]},  # 10
        {"op": "sum", "inputs": [10]},  # 11
        {"op": "add", "inputs": [8, 11]},  # 12
    ]
    return nodes, 12


def nonlinear_spec() -> tuple[list[dict], int]:
    """f(x) = sin(x0) * exp(x1) + x0^2 * x1 for x in R^2."""
    nodes = [
        {"op": "input", "params": {"name": "x", "shape": [2]}},  # 0
        {"op": "take", "inputs": [0], "params": {"index": 0}},  # 1: x0
        {"op": "take", "inputs": [0], "params": {"index": 1}},  # 2: x1
        {"op": "sin", "inputs": [1]},  # 3
        {"op": "exp", "inputs": [2]},  # 4
        {"op": "mul", "inputs": [3, 4]},  # 5
        {"op": "pow_const", "inputs": [1], "params": {"exponent": 2.0}},  # 6
        {"op": "mul", "inputs": [6, 2]},  # 7
        {"op": "add", "inputs": [5, 7]},  # 8
    ]
    return nodes, 8


def sharing_spec() -> tuple[list[dict], int]:
    """z = x0*x1 + sin(x0); f = z^2 + z. Node 5 (z) is shared by 6 and 7."""
    nodes = [
        {"op": "input", "params": {"name": "x", "shape": [2]}},  # 0
        {"op": "take", "inputs": [0], "params": {"index": 0}},  # 1: x0
        {"op": "take", "inputs": [0], "params": {"index": 1}},  # 2: x1
        {"op": "mul", "inputs": [1, 2]},  # 3
        {"op": "sin", "inputs": [1]},  # 4
        {"op": "add", "inputs": [3, 4]},  # 5: z
        {"op": "mul", "inputs": [5, 5]},  # 6: z^2 (z consumed twice)
        {"op": "add", "inputs": [6, 5]},  # 7: z^2 + z
    ]
    return nodes, 7


def two_slot_spec() -> tuple[list[dict], int]:
    """f(w, b) = sum(w*w) + b*b with w in R^2 and scalar b."""
    nodes = [
        {"op": "input", "params": {"name": "w", "shape": [2]}},  # 0
        {"op": "input", "params": {"name": "b", "shape": []}},  # 1
        {"op": "mul", "inputs": [0, 0]},  # 2
        {"op": "sum", "inputs": [2]},  # 3
        {"op": "mul", "inputs": [1, 1]},  # 4
        {"op": "add", "inputs": [3, 4]},  # 5
    ]
    return nodes, 5


def unary_spec(op: str, n: int = 2) -> tuple[list[dict], int]:
    """f(x) = sum(op(x)) for x in R^n."""
    nodes = [
        {"op": "input", "params": {"name": "x", "shape": [n]}},  # 0
        {"op": op, "inputs": [0]},  # 1
        {"op": "sum", "inputs": [1]},  # 2
    ]
    return nodes, 2


@pytest.fixture()
def quad_point() -> np.ndarray:
    return np.array([0.4, -1.1, 2.3])


@pytest.fixture()
def quad_direction() -> np.ndarray:
    return np.array([1.0, -0.5, 0.75])


@pytest.fixture()
def nl_point() -> np.ndarray:
    return np.array([0.7, -0.4])


@pytest.fixture()
def nl_direction() -> np.ndarray:
    return np.array([-0.8, 1.3])
