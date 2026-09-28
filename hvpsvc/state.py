"""Training/evaluation state.

A :class:`FunctionState` binds a parsed primal expression, its input layout
and the numerical/nonsmooth configuration together with the current
evaluation point ("parameters"). The point is **versioned**: every
``set_point`` bumps the version and invalidates cached derivative graphs.

Attempting to evaluate value / gradient / HVP before a point exists raises
:class:`~hvpsvc.errors.StateConflictError`; optimistic-concurrency callers
may also pin ``expected_version``.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .autodiff import GradientGraph, build_gradient_graph
from .errors import InputError, StateConflictError
from .graph import Budget, ExprGraph, NonsmoothConfig, build_primal_from_spec
from .tensor import InputLayout


@dataclass
class FunctionState:
    state_id: str
    layout: InputLayout
    graph: ExprGraph
    output: str
    nonsmooth: NonsmoothConfig
    budget_limits: dict[str, int]
    spec: dict[str, Any]
    version: int = 0
    point: np.ndarray | None = None
    point_preview: dict[str, Any] | None = None
    _grad: GradientGraph | None = field(default=None, repr=False)
    _grad_version: int = -1
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def set_point(self, values: dict[str, Any], *,
                  expected_version: int | None = None) -> int:
        with self._lock:
            if expected_version is not None and expected_version != self.version:
                raise StateConflictError(
                    f"stale state: expected version {expected_version}, "
                    f"current version is {self.version}",
                    detail={"expected_version": expected_version,
                            "current_version": self.version})
            x = self.layout.pack(values)
            self.point = x
            self.point_preview = self.layout.unpack(x)
            self.version += 1
            self._grad = None
            self._grad_version = -1
            return self.version

    def require_point(self) -> np.ndarray:
        if self.point is None:
            raise StateConflictError(
                "no evaluation point set for this state; call set-point first",
                detail={"state_id": self.state_id})
        return self.point

    def new_budget(self, overrides: dict[str, int] | None = None) -> Budget:
        limits = dict(self.budget_limits)
        if overrides:
            limits.update({k: int(v) for k, v in overrides.items()})
        return Budget(
            max_nodes=int(limits.get("max_nodes", 200_000)),
            max_evals=int(limits.get("max_evals", 2_000_000)),
        )

    def gradient_graph(self, budget: Budget) -> GradientGraph:
        """Build (once per point version) and cache the symbolic gradient."""
        with self._lock:
            x = self.require_point()
            if self._grad is None or self._grad_version != self.version:
                self._grad = build_gradient_graph(
                    self.graph, self.output, x, self.layout,
                    budget, self.nonsmooth)
                self._grad_version = self.version
            return self._grad


class StateStore:
    """In-memory registry of function states (synthetic local service)."""

    def __init__(self) -> None:
        self._states: dict[str, FunctionState] = {}
        self._lock = threading.RLock()

    def create(
        self,
        spec: dict[str, Any],
        *,
        nonsmooth: NonsmoothConfig | None = None,
        budget_limits: dict[str, int] | None = None,
    ) -> FunctionState:
        norm = _normalize_spec(spec)
        layout = InputLayout.from_specs(norm.get("variables", []))
        parse_budget = Budget(
            max_nodes=int((budget_limits or {}).get("max_nodes", 200_000)))
        graph, output = build_primal_from_spec(
            norm, layout, budget=parse_budget)
        state_id = uuid.uuid4().hex[:12]
        state = FunctionState(
            state_id=state_id,
            layout=layout,
            graph=graph,
            output=output,
            nonsmooth=nonsmooth or NonsmoothConfig(),
            budget_limits=budget_limits or {},
            spec=norm,
        )
        with self._lock:
            self._states[state_id] = state
        return state

    def get(self, state_id: str) -> FunctionState:
        with self._lock:
            state = self._states.get(state_id)
        if state is None:
            raise StateConflictError(f"unknown state_id {state_id!r}",
                                     detail={"state_id": state_id})
        return state

    def delete(self, state_id: str) -> None:
        with self._lock:
            if state_id not in self._states:
                raise StateConflictError(f"unknown state_id {state_id!r}",
                                         detail={"state_id": state_id})
            del self._states[state_id]

    def __len__(self) -> int:
        return len(self._states)


def _normalize_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """Accept the nested API shape {variables, expression:{nodes,output}}
    and return the flat shape {variables, nodes, output} used by the core."""
    if not isinstance(spec, dict):
        raise InputError("function spec must be an object")
    norm = dict(spec)
    expr = norm.pop("expression", None)
    if expr is not None:
        if not isinstance(expr, dict):
            raise InputError("'expression' must be an object")
        norm.setdefault("nodes", expr.get("nodes"))
        norm.setdefault("output", expr.get("output"))
    return norm
