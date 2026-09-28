"""Training state: parameter values, gradients and lifecycle rules.

The executor never mutates a :class:`TrainingState` in place while a run is in
progress: a run produces an immutable :class:`RunState` result that the caller
applies explicitly.  Lifecycle conflicts (double apply, apply after failed
run) raise :class:`StateConflictError`, distinct from bad inputs.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Mapping, Tuple

import numpy as np

from .errors import InvalidInputError, StateConflictError
from .tensor import SUPPORTED_DTYPE


class RunStatus(str, Enum):
    PLANNED = "planned"
    EXECUTED = "executed"
    FAILED = "failed"


@dataclass(frozen=True)
class ParameterInit:
    shape: Tuple[int, ...]
    seed: int  # per-parameter seed; values are synthesised locally


@dataclass(frozen=True)
class RunState:
    run_id: str
    status: RunStatus
    loss: float
    grads: Mapping[str, np.ndarray]
    """Gradient per *parameter* node id (views; caller copies if retained)."""
    recomputed_nodes: Tuple[str, ...]
    forward_flops: int
    recompute_flops: int
    backward_flops: int
    peak_memory: int
    emitted_side_effects: int
    rng_replay_ok: bool
    failure: object = None


class TrainingState:
    """Holds parameter values and the SGD step counter."""

    def __init__(self, parameter_inits: Mapping[str, ParameterInit]) -> None:
        if not parameter_inits:
            raise InvalidInputError(
                "training state requires at least one parameter",
                code="E_STATE_NO_PARAMS",
            )
        self._shapes: Dict[str, Tuple[int, ...]] = {}
        self._values: Dict[str, np.ndarray] = {}
        for pid, init in parameter_inits.items():
            if not isinstance(pid, str) or not pid:
                raise InvalidInputError(
                    "parameter id must be a non-empty string",
                    code="E_STATE_BAD_PARAM_ID",
                )
            if pid in self._values:
                raise InvalidInputError(
                    f"duplicate parameter {pid!r}",
                    code="E_STATE_DUP_PARAM",
                    context={"id": pid},
                )
            rng = np.random.RandomState(init.seed)
            # Small, well-scaled synthetic values (Xavier-ish for 2-D).
            if len(init.shape) == 2:
                m, n = init.shape
                scale = (6.0 / (m + n)) ** 0.5
            else:
                scale = 0.1
            value = rng.uniform(-scale, scale, size=init.shape).astype(
                SUPPORTED_DTYPE
            )
            self._values[pid] = value
            self._shapes[pid] = tuple(init.shape)
        self._step = 0
        self._applied_run_ids: set[str] = set()

    @property
    def step(self) -> int:
        return self._step

    def parameter_ids(self) -> Tuple[str, ...]:
        return tuple(self._values)

    def shape_of(self, pid: str) -> Tuple[int, ...]:
        return self._shapes[pid]

    def values(self) -> Mapping[str, np.ndarray]:
        # Read-only view; updates go through apply_run.
        return {k: v.view() for k, v in self._values.items()}

    def get(self, pid: str) -> np.ndarray:
        return self._values[pid]

    def apply_run(self, run: RunState, lr: float = 0.1) -> None:
        """Apply one SGD step from a completed run; exactly once per run."""

        if run.status is not RunStatus.EXECUTED:
            raise StateConflictError(
                "only an executed run can be applied",
                code="E_STATE_RUN_NOT_EXECUTED",
                context={"run_id": run.run_id, "status": run.status.value},
            )
        if run.run_id in self._applied_run_ids:
            raise StateConflictError(
                "run has already been applied",
                code="E_STATE_RUN_DOUBLE_APPLY",
                context={"run_id": run.run_id, "step": self._step},
            )
        if not isinstance(lr, (int, float)) or isinstance(lr, bool) or lr <= 0:
            raise InvalidInputError(
                "learning rate must be a positive number",
                code="E_STATE_LR_INVALID",
                context={"lr": lr},
            )
        missing = set(self._values) - set(run.grads)
        if missing:
            raise StateConflictError(
                "run gradients do not cover all parameters",
                code="E_STATE_GRAD_MISMATCH",
                context={"missing": sorted(missing)},
            )
        # New arrays, never mutate in place.
        new_values = {
            pid: (self._values[pid] - float(lr) * np.asarray(run.grads[pid]))
            for pid in self._values
        }
        for pid, arr in new_values.items():
            if not np.all(np.isfinite(arr)):
                raise StateConflictError(
                    "non-finite parameter after update",
                    code="E_STATE_NONFINITE",
                    context={"parameter": pid},
                )
        self._values = new_values
        self._step += 1
        self._applied_run_ids.add(run.run_id)


def check_gradient_shapes(grads: Mapping[str, np.ndarray],
                          state: TrainingState) -> None:
    for pid, g in grads.items():
        if pid not in state.parameter_ids():
            raise InvalidInputError(
                f"gradient for unknown parameter {pid!r}",
                code="E_STATE_GRAD_UNKNOWN_PARAM",
                context={"id": pid},
            )
        if tuple(np.asarray(g).shape) != state.shape_of(pid):
            raise InvalidInputError(
                "gradient shape does not match parameter",
                code="E_STATE_GRAD_SHAPE",
                context={
                    "id": pid,
                    "grad_shape": list(np.asarray(g).shape),
                    "param_shape": list(state.shape_of(pid)),
                },
            )
