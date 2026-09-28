"""Training state: parameters, gradient buffers, optimizer slots, phase rules.

:class:`TrainingState` owns the *persistent across steps* tensors. Those
tensors are external storage from the pool planner's point of view: a training
graph takes parameters as feeds and emits gradients as outputs, and this class
validates shape/dtype contracts between the two, applies optimizer updates, and
guards legal phase transitions.

Phases::

    CREATED --declare_parameter*--> CREATED
    CREATED --prepare()-----------> PREPARED
    PREPARED --begin_step() ------> PREPARED (a step is in flight)
    PREPARED --close() -----------> CLOSED   (only with no step in flight)

Illegal transitions raise :class:`StateConflictError`; malformed caller data
raises :class:`InputValidationError`; non-finite gradients raise
:class:`ComputationError`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

import numpy as np

from .errors import (
    ComputationError,
    InputValidationError,
    StateConflictError,
)
from .tensor import Shape, TensorType, shape_from


class Phase(str, Enum):
    CREATED = "created"
    PREPARED = "prepared"
    CLOSED = "closed"


_OPTIMIZERS = ("sgd", "adam")


@dataclass(frozen=True)
class ParameterSpec:
    name: str
    tensor_type: TensorType
    shape: Shape


class TrainingState:
    """Parameters and optimizer slots for one synthetic training loop."""

    def __init__(self, optimizer: str = "sgd") -> None:
        if optimizer not in _OPTIMIZERS:
            raise InputValidationError(
                "unsupported optimizer",
                optimizer=optimizer,
                supported=list(_OPTIMIZERS),
            )
        self._optimizer = optimizer
        self._phase = Phase.CREATED
        self._specs: dict[str, ParameterSpec] = {}
        self._params: dict[str, np.ndarray] = {}
        self._grad_bufs: dict[str, np.ndarray] = {}
        self._slots: dict[str, dict[str, np.ndarray]] = {}
        self._step_count = 0
        self._step_open = False

    # ------------------------------------------------------------------ #
    # Construction
    # ------------------------------------------------------------------ #

    @property
    def phase(self) -> Phase:
        return self._phase

    @property
    def optimizer(self) -> str:
        return self._optimizer

    @property
    def step_count(self) -> int:
        return self._step_count

    @property
    def parameter_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._specs))

    def declare_parameter(self, name: str, dtype: str, shape: Any) -> None:
        self._require_phase(Phase.CREATED, action="declare_parameter")
        if not isinstance(name, str) or not name:
            raise InputValidationError("parameter name must be non-empty string")
        if name in self._specs:
            raise StateConflictError(
                "parameter already declared", parameter=name
            )
        s = shape_from(shape)
        spec = ParameterSpec(name, TensorType(dtype, s.rank), s)
        self._specs[name] = spec

    def prepare(self, seed: int = 0) -> None:
        """Allocate parameter/gradient/slot storage with seeded init values."""
        self._require_phase(Phase.CREATED, action="prepare")
        if not self._specs:
            raise StateConflictError(
                "cannot prepare a training state with no declared parameters"
            )
        rng = np.random.default_rng(seed)
        for name, spec in self._specs.items():
            arr = rng.standard_normal(spec.shape.as_tuple()).astype(
                spec.tensor_type.numpy_dtype, copy=False
            )
            self._params[name] = arr
            self._grad_bufs[name] = np.empty_like(arr)
            slots: dict[str, np.ndarray] = {}
            if self._optimizer == "adam":
                slots["m"] = np.zeros_like(arr)
                slots["v"] = np.zeros_like(arr)
            self._slots[name] = slots
        self._phase = Phase.PREPARED

    # ------------------------------------------------------------------ #
    # Runtime use
    # ------------------------------------------------------------------ #

    def specs(self) -> dict[str, ParameterSpec]:
        return dict(self._specs)

    def parameter_array(self, name: str) -> np.ndarray:
        self._require_prepared("read_parameter")
        self._require_param(name)
        return self._params[name]

    def external_feeds(self) -> dict[str, np.ndarray]:
        """Arrays to inject as graph feeds (parameter tensors).

        Returns the live arrays deliberately: a training graph reads the
        current parameter values. Callers must not resize them.
        """
        self._require_prepared("external_feeds")
        return dict(self._params)

    def begin_step(self) -> int:
        """Open a training step; a second open step is a state conflict."""
        self._require_prepared("begin_step")
        if self._step_open:
            raise StateConflictError(
                "a training step is already in progress",
                step=self._step_count + 1,
            )
        self._step_open = True
        return self._step_count + 1

    def apply_gradients(
        self,
        gradients: dict[str, np.ndarray],
        lr: float,
        beta1: float = 0.9,
        beta2: float = 0.999,
        eps: float = 1e-8,
    ) -> None:
        """Validate gradient contract and apply one optimizer update."""
        self._require_prepared("apply_gradients")
        if not self._step_open:
            raise StateConflictError(
                "apply_gradients called without begin_step"
            )
        if not isinstance(gradients, dict) or not gradients:
            raise InputValidationError(
                "gradients must be a non-empty dict of name -> ndarray"
            )
        if set(gradients) != set(self._specs):
            missing = sorted(set(self._specs) - set(gradients))
            extra = sorted(set(gradients) - set(self._specs))
            raise StateConflictError(
                "gradient set does not match declared parameters",
                missing=missing, extra=extra,
            )
        if lr <= 0:
            raise InputValidationError("learning rate must be positive", lr=lr)

        for name, grad in gradients.items():
            spec = self._specs[name]
            if not isinstance(grad, np.ndarray):
                raise InputValidationError(
                    "gradient must be a numpy ndarray", parameter=name
                )
            if grad.shape != spec.shape.as_tuple():
                raise StateConflictError(
                    "gradient shape does not match parameter "
                    "(dynamic graph output diverged from state)",
                    parameter=name,
                    expected=list(spec.shape.as_tuple()),
                    got=list(grad.shape),
                )
            desired = spec.tensor_type.numpy_dtype
            if grad.dtype != desired:
                raise StateConflictError(
                    "gradient dtype does not match parameter",
                    parameter=name,
                    expected=spec.tensor_type.dtype,
                    got=str(grad.dtype),
                )
            if not np.all(np.isfinite(grad)):
                raise ComputationError(
                    "gradient contains non-finite values",
                    parameter=name,
                    non_finite=int(np.size(grad) - np.count_nonzero(np.isfinite(grad))),
                )
            self._grad_bufs[name] = np.array(grad, dtype=desired, copy=True)

        self._step_count += 1
        if self._optimizer == "sgd":
            for name in self._specs:
                self._params[name] = self._params[name] - lr * self._grad_bufs[name]
        else:
            t = self._step_count
            for name in self._specs:
                p = self._params[name]
                g = self._grad_bufs[name]
                m = self._slots[name]["m"]
                v = self._slots[name]["v"]
                m[:] = beta1 * m + (1.0 - beta1) * g
                v[:] = beta2 * v + (1.0 - beta2) * (g * g)
                mhat = m / (1.0 - beta1 ** t)
                vhat = v / (1.0 - beta2 ** t)
                self._params[name] = p - lr * mhat / (np.sqrt(vhat) + eps)
        self._step_open = False

    def close(self) -> None:
        self._require_prepared("close")
        if self._step_open:
            raise StateConflictError(
                "cannot close training state while a step is in progress"
            )
        self._phase = Phase.CLOSED

    # ------------------------------------------------------------------ #
    # Reporting
    # ------------------------------------------------------------------ #

    def state_bytes(self) -> dict[str, int]:
        """Resident bytes for parameters, gradient buffers and optimizer slots."""
        def total(arrays: dict[str, np.ndarray]) -> int:
            return sum(int(a.nbytes) for a in arrays.values())

        slot_bytes = 0
        for buckets in self._slots.values():
            slot_bytes += total(buckets)
        return {
            "parameters": total(self._params),
            "gradients": total(self._grad_bufs),
            "optimizer_slots": slot_bytes,
            "total": total(self._params) + total(self._grad_bufs) + slot_bytes,
        }

    def snapshot(self) -> dict[str, np.ndarray]:
        """Deep copy of current parameters (used by tests/replay)."""
        self._require_prepared("snapshot")
        return {n: a.copy() for n, a in self._params.items()}

    # ------------------------------------------------------------------ #

    def _require_phase(self, phase: Phase, action: str) -> None:
        if self._phase != phase:
            raise StateConflictError(
                f"illegal action {action!r} in phase {self._phase.value!r}",
                action=action, phase=self._phase.value,
                required_phase=phase.value,
            )

    def _require_prepared(self, action: str) -> None:
        if self._phase != Phase.PREPARED:
            raise StateConflictError(
                f"illegal action {action!r} in phase {self._phase.value!r}",
                action=action, phase=self._phase.value,
                required_phase=Phase.PREPARED.value,
            )

    def _require_param(self, name: str) -> None:
        if name not in self._specs:
            raise InputValidationError(
                "unknown parameter",
                name=name, known=sorted(self._specs),
            )
