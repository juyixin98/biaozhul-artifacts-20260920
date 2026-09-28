"""Server configuration and compute budgets.

Budgets are part of the error contract: exceeding them raises
``resource_exhausted`` errors with the observed vs. allowed values in the
details payload, so callers can diagnose and retry with a smaller problem
or an explicitly raised (server-capped) budget.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class ServiceConfig:
    # Hard caps enforced by the server.
    max_graph_nodes: int
    max_eval_nodes: int
    default_time_budget_ms: float | None


@dataclass(frozen=True)
class Budget:
    """Effective budget for one HVP evaluation."""

    max_eval_nodes: int
    time_budget_ms: float | None


def load_config() -> ServiceConfig:
    time_budget = _env_int("HVP_TIME_BUDGET_MS", 0)
    return ServiceConfig(
        max_graph_nodes=_env_int("HVP_MAX_GRAPH_NODES", 20_000),
        max_eval_nodes=_env_int("HVP_MAX_EVAL_NODES", 200_000),
        default_time_budget_ms=float(time_budget) if time_budget > 0 else None,
    )
