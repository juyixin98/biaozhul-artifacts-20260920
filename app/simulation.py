"""Deterministic closed-loop simulation harness (no hardware).

Runs the bounded MPC against the double-integrator plant for a configurable,
finite number of time steps.  The plant receives an optional deterministic
synthetic disturbance that the controller does not see, which is exactly the
point: tracking and fallback behaviour can be checked under a known, repeatable
mismatch.

Every time step is recorded so tests can verify state updates and constraint
residuals one step at a time.
"""

from __future__ import annotations

import dataclasses

import numpy as np

from .disturbance import deterministic_acceleration
from .mpc import BoundedMPC, MpcResult, MpcStatus
from .model import DoubleIntegrator


@dataclasses.dataclass
class StepRecord:
    step: int
    time: float
    state_before: list[float]
    reference: list[float]
    control: float
    fallback: bool
    status: str
    reason: str
    disturbance: float
    state_after: list[float]
    velocity_violation: float   # v bound violation after the step
    input_violation: float      # |applied u| - u_max (>=0 only on violation)
    solve_residuals: dict

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


def simulate(
    mpc: BoundedMPC,
    x0: list[float] | np.ndarray,
    reference: list[float] | np.ndarray,
    steps: int,
    disturbance_amplitude: float = 0.0,
    disturbance_seed: int = 0,
) -> dict:
    """Run ``steps`` closed-loop steps.

    ``reference`` is the full target trajectory (one position per simulated
    step); shorter arrays are extended by holding the last value.  At each
    step the MPC horizon window is the next ``N`` targets.
    """
    if steps < 1:
        raise ValueError("steps must be >= 1")
    plant: DoubleIntegrator = mpc.model
    N = mpc.N
    ref = np.asarray(reference, dtype=float).reshape(-1)
    if ref.size == 0:
        raise ValueError("reference must contain at least one target")
    if ref.size < steps + N:
        ref = np.concatenate(
            [ref, np.full(steps + N - ref.size, ref[-1])]
        )

    x = np.asarray(x0, dtype=float).reshape(plant.n)
    u_prev = 0.0
    records: list[StepRecord] = []

    for k in range(steps):
        window = ref[k + 1 : k + 1 + N]  # targets for k+1 .. k+N
        d = deterministic_acceleration(k, disturbance_amplitude, disturbance_seed)
        x_before = x.copy()

        result: MpcResult = mpc.solve(x, window, u_prev)
        u = result.control

        x = plant.step(x, u, d)
        u_prev = u

        records.append(
            StepRecord(
                step=k,
                time=(k + 1) * plant.dt,
                state_before=x_before.tolist(),
                reference=window.tolist(),
                control=float(u),
                fallback=result.fallback,
                status=result.status.value,
                reason=result.reason,
                disturbance=d,
                state_after=x.tolist(),
                velocity_violation=float(
                    max(0.0, abs(x[1]) - mpc.cfg.v_max)
                ),
                input_violation=float(
                    max(0.0, abs(u) - mpc.cfg.u_max)
                ),
                solve_residuals=result.residuals.as_dict(),
            )
        )

    statuses = sorted({r.status for r in records})
    return {
        "dt": plant.dt,
        "horizon": N,
        "steps": steps,
        "initial_state": np.asarray(x0, dtype=float).reshape(plant.n).tolist(),
        "disturbance_amplitude": disturbance_amplitude,
        "disturbance_seed": disturbance_seed,
        "v_max": mpc.cfg.v_max,
        "u_max": mpc.cfg.u_max,
        "final_state": x.tolist(),
        "n_fallback": sum(r.fallback for r in records),
        "statuses": statuses,
        "records": [r.as_dict() for r in records],
    }


def assert_step_consistency(mpc: BoundedMPC, report: dict) -> None:
    """Re-derive every state transition from the model; raise on mismatch.

    Used by tests as an independent check of the recorded rollout.
    """
    plant = mpc.model
    prev = np.array(report["initial_state"], dtype=float)
    for rec in report["records"]:
        d = rec["disturbance"]
        expected = plant.step(prev, rec["control"], d)
        actual = np.array(rec["state_after"], dtype=float)
        if np.max(np.abs(expected - actual)) > 1e-10:
            raise AssertionError(
                f"step {rec['step']}: state update inconsistent "
                f"(expected {expected}, got {actual})"
            )
        prev = actual
