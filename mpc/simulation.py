"""Deterministic closed-loop simulation harness (no hardware).

At each simulated step the controller solves the MPC problem from the
*measured* state, and the plant is propagated with the exact discretized
model plus a deterministic disturbance. If the controller falls back, the
fallback control is applied and the event is recorded with its reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .config import MPCConfig
from .disturbance import DisturbanceSpec, apply_disturbance
from .mpc import MPCController, MPCResult


@dataclass
class StepRecord:
    k: int
    t: float
    state: list[float]
    reference: list[float]
    control: float
    status: str
    fallback: bool
    reason: str
    solve_time_s: float
    residuals: dict
    disturbance: list[float]

    def as_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class RolloutResult:
    steps: list[StepRecord] = field(default_factory=list)
    final_state: list[float] = field(default_factory=list)
    fallback_count: int = 0
    fallback_reasons: dict = field(default_factory=dict)
    max_velocity: float = 0.0
    max_acceleration: float = 0.0
    max_velocity_violation: float = 0.0
    max_acceleration_violation: float = 0.0

    def as_dict(self) -> dict:
        return {
            "steps": [s.as_dict() for s in self.steps],
            "final_state": self.final_state,
            "fallback_count": self.fallback_count,
            "fallback_reasons": self.fallback_reasons,
            "max_velocity": self.max_velocity,
            "max_acceleration": self.max_acceleration,
            "max_velocity_violation": self.max_velocity_violation,
            "max_acceleration_violation": self.max_acceleration_violation,
        }


def constant_reference(value: tuple[float, float], n: int) -> np.ndarray:
    return np.tile(np.asarray(value, dtype=float), (n, 1))


def step_reference(before: tuple[float, float], after: tuple[float, float],
                   change_step: int, n_steps: int,
                   horizon: int) -> list[np.ndarray]:
    """Reference windows with an abrupt set-point change at change_step.

    Returns one (N+1, 2) window per simulated step: all rows ``before`` up
    to (but not including) change_step, ``after`` from change_step on.
    """
    before, after = map(np.asarray, (before, after))
    total = n_steps + horizon
    full = np.where(
        (np.arange(total) >= change_step)[:, None],
        after, before,
    )
    return [full[k:k + horizon + 1].copy() for k in range(n_steps)]


def rollout(controller: MPCController,
            x0,
            references: np.ndarray | list[np.ndarray],
            n_steps: int,
            disturbance: DisturbanceSpec | None = None) -> RolloutResult:
    """Run n_steps controlled steps.

    ``references`` is either a single (N+1, 2) window held constant, or a
    list of n_steps such windows (enables reference changes mid-run).
    """
    cfg: MPCConfig = controller.config
    N = cfg.horizon
    disturbance = disturbance or DisturbanceSpec()

    if isinstance(references, np.ndarray) and references.ndim == 2:
        windows = [references.copy() for _ in range(n_steps)]
    else:
        windows = list(references)
        if len(windows) != n_steps:
            raise ValueError("reference windows must equal n_steps")

    x = np.asarray(x0, dtype=float).reshape(2)
    u_prev = 0.0
    out = RolloutResult()

    for k in range(n_steps):
        ref = np.asarray(windows[k], dtype=float).reshape(N + 1, 2)
        result: MPCResult = controller.solve(x, ref, u_prev=u_prev)

        w = np.asarray(apply_disturbance(disturbance, k, cfg.dt), dtype=float)
        record = StepRecord(
            k=k, t=k * cfg.dt,
            state=[float(x[0]), float(x[1])],
            reference=[float(ref[0, 0]), float(ref[0, 1])],
            control=result.control,
            status=result.status,
            fallback=result.fallback,
            reason=result.reason.value,
            solve_time_s=result.solve_time_s,
            residuals=dict(result.residuals),
            disturbance=[float(w[0]), float(w[1])],
        )
        out.steps.append(record)
        if result.fallback:
            out.fallback_count += 1
            out.fallback_reasons[result.reason.value] = (
                out.fallback_reasons.get(result.reason.value, 0) + 1
            )

        # plant propagation with the (possibly fallback) control + disturbance
        x = controller.model.step(x, result.control, disturbance=w)
        u_prev = result.control

        out.max_velocity = max(out.max_velocity, abs(float(x[1])))
        out.max_acceleration = max(out.max_acceleration, abs(result.control))
        out.max_velocity_violation = max(
            out.max_velocity_violation,
            max(0.0, abs(float(x[1])) - cfg.v_max),
        )
        out.max_acceleration_violation = max(
            out.max_acceleration_violation,
            max(0.0, abs(result.control) - cfg.a_max),
        )

    out.final_state = [float(x[0]), float(x[1])]
    return out
