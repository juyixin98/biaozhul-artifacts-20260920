"""Bounded MPC controller: solve -> verify -> or conservative fallback.

Contract (deliberately strict):

1. The QP matrices are rebuilt every call *from the model* (see qp.py).
2. A returned control is marked ``ok`` ONLY when OSQP reports optimality
   AND the solution contains no NaN/Inf AND independently reconstructed
   predictions satisfy every hard constraint within a residual tolerance.
3. On timeout, infeasibility, solver error, non-optimal status or failed
   residual verification the controller returns an EXPLICIT conservative
   fallback with a machine-readable reason. The fallback is computed solely
   from the current measured state (a saturated velocity-feedback braking
   law); a previously solved, unverified or stale control is NEVER reused.
"""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field

import numpy as np
import osqp

from .config import MPCConfig
from .model import DoubleIntegrator
from .qp import QPBuilder, QPMatrices


class FallbackReason(str, enum.Enum):
    NONE = "none"
    INITIAL_STATE_INFEASIBLE = "initial_state_infeasible"
    NONFINITE_STATE = "nonfinite_state"
    SOLVER_TIMEOUT = "solver_timeout"
    SOLVER_INFEASIBLE = "solver_infeasible"
    SOLVER_ERROR = "solver_error"
    SOLVER_NON_OPTIMAL = "solver_non_optimal"
    SOLUTION_NONFINITE = "solution_nonfinite"
    CONSTRAINT_RESIDUAL = "constraint_residual"
    FORCED_FAILURE = "forced_failure"


@dataclass
class MPCResult:
    status: str                      # "ok" | "fallback"
    control: float                   # applied u_0 (always finite, |u|<=a_max)
    fallback: bool
    reason: FallbackReason
    reason_detail: str
    solve_time_s: float
    osqp_status: str | None = None
    objective: float | None = None
    control_sequence: list[float] = field(default_factory=list)
    predicted_states: list[list[float]] = field(default_factory=list)
    residuals: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "control": self.control,
            "fallback": self.fallback,
            "reason": self.reason.value,
            "reason_detail": self.reason_detail,
            "solve_time_s": self.solve_time_s,
            "osqp_status": self.osqp_status,
            "objective": self.objective,
            "control_sequence": self.control_sequence,
            "predicted_states": self.predicted_states,
            "residuals": self.residuals,
        }


# Residual tolerance for post-solve verification (absolute, SI units).
RESIDUAL_TOL = 1e-5


class MPCController:
    def __init__(self, config: MPCConfig | None = None):
        self.config = config or MPCConfig()
        self.model = DoubleIntegrator(self.config)
        self.builder = QPBuilder(self.model, self.config)
        self._solver: osqp.OSQP | None = None
        self._solver_matrices: tuple | None = None
        # Test hook: force a failure class deterministically (never used by
        # the HTTP API). One of: None, "timeout", "infeasible", "error",
        # "nonfinite", "nonoptimal".
        self.force_fail: str | None = None

    # ------------------------------------------------------------------ #
    # Initial-state feasibility
    # ------------------------------------------------------------------ #
    def initial_state_violations(self, x0: np.ndarray) -> dict:
        """Check state bounds implied by the hard constraints at step 0.

        Only the velocity component is a state constraint (|v| <= v_max).
        Position is unconstrained by design. Also rejects NaN/Inf.
        """
        x0 = np.asarray(x0, dtype=float).reshape(2)
        out: dict = {}
        if not np.all(np.isfinite(x0)):
            out["nonfinite"] = True
            return out
        v = float(x0[1])
        if v > self.config.v_max + RESIDUAL_TOL:
            out["v_above_max"] = v
        if v < -self.config.v_max - RESIDUAL_TOL:
            out["v_below_min"] = v
        return out

    # ------------------------------------------------------------------ #
    # Conservative fallback (never trusts old solutions)
    # ------------------------------------------------------------------ #
    def fallback_control(self, x0: np.ndarray) -> float:
        """Saturated braking: u = sat(-K_brake * v), K*v_max <= a_max.

        Requires only the current measured state. Guarantees |u| <= a_max by
        construction and is finite even for NaN input (zero control).
        """
        x0 = np.asarray(x0, dtype=float).reshape(2)
        v = float(x0[1]) if np.isfinite(x0[1]) else 0.0
        u = -self.config.k_brake * v
        return float(np.clip(u, -self.config.a_max, self.config.a_max))

    def _fallback_result(self, reason: FallbackReason, detail: str,
                         x0: np.ndarray, elapsed: float,
                         osqp_status: str | None = None) -> MPCResult:
        return MPCResult(
            status="fallback",
            control=self.fallback_control(x0),
            fallback=True,
            reason=reason,
            reason_detail=detail,
            solve_time_s=elapsed,
            osqp_status=osqp_status,
        )

    # ------------------------------------------------------------------ #
    # QP solve
    # ------------------------------------------------------------------ #
    def _setup_solver(self, qp: QPMatrices, force_nonoptimal: bool = False):
        cfg = self.config
        solver = osqp.OSQP()
        kwargs = dict(
            eps_abs=cfg.osqp_eps_abs,
            eps_rel=cfg.osqp_eps_rel,
            max_iter=cfg.osqp_max_iter,
            time_limit=cfg.osqp_time_limit,
            polishing=cfg.osqp_polish,
            verbose=cfg.osqp_verbose,
        )
        if force_nonoptimal:
            # Deliberately cripple the solver so it exits unsolved.
            kwargs["max_iter"] = 1
            kwargs["polishing"] = False
        solver.setup(qp.P, qp.q, qp.A, qp.lower, qp.upper, **kwargs)
        return solver

    def _verify_solution(self, qp: QPMatrices, z: np.ndarray,
                         x0: np.ndarray, u_prev: float) -> tuple[bool, dict]:
        """Independently reconstruct and check all hard constraints."""
        u_seq, states = self.builder.reconstruct(qp, x0, z, u_prev)

        acc_viol = float(max(
            0.0,
            np.max(np.abs(u_seq)) - self.config.a_max,
        ))
        vel_viol = float(max(
            0.0,
            np.max(np.abs(states[1:, 1])) - self.config.v_max,
        ))
        # equality residual of the QP inequality rows: A z within [l, u]
        Az = qp.A @ z
        low_viol = float(np.max(qp.lower - Az)) if qp.A.shape[0] else 0.0
        up_viol = float(np.max(Az - qp.upper)) if qp.A.shape[0] else 0.0
        row_viol = max(0.0, low_viol, up_viol)

        res = {
            "a_max_violation": acc_viol,
            "v_max_violation": vel_viol,
            "constraint_row_violation": row_viol,
        }
        ok = (acc_viol <= RESIDUAL_TOL and vel_viol <= RESIDUAL_TOL
              and row_viol <= RESIDUAL_TOL)
        return ok, res

    def solve(self, x0, reference, u_prev: float = 0.0) -> MPCResult:
        """Solve one MPC problem for current state and reference trajectory.

        Parameters
        ----------
        x0 : array-like, shape (2,)
            Current measured state [position, velocity].
        reference : array-like, shape (N+1, 2)
            Reference states [p_ref, v_ref] over the horizon.
        u_prev : float
            Last *actually applied* acceleration (for the Du formulation).
        """
        x0 = np.asarray(x0, dtype=float).reshape(2)
        start = time.perf_counter()

        def fail(reason, detail, status=None):
            return self._fallback_result(
                reason, detail, x0, time.perf_counter() - start, status
            )

        # --- hard pre-checks on the measured state ---
        if not np.all(np.isfinite(x0)):
            return fail(FallbackReason.NONFINITE_STATE,
                        "current state contains NaN or Inf")
        v0 = float(x0[1])
        if abs(v0) > self.config.v_max + RESIDUAL_TOL:
            return fail(
                FallbackReason.INITIAL_STATE_INFEASIBLE,
                f"initial velocity {v0:.6g} violates |v| <= {self.config.v_max}",
            )
        if not np.isfinite(u_prev):
            return fail(FallbackReason.NONFINITE_STATE,
                        "u_prev is not finite")

        reference = np.asarray(reference, dtype=float)
        if reference.shape != (self.config.horizon + 1, 2):
            raise ValueError(
                f"reference must have shape ({self.config.horizon + 1}, 2), "
                f"got {reference.shape}"
            )
        if not np.all(np.isfinite(reference)):
            raise ValueError("reference contains NaN or Inf")

        qp = self.builder.build(x0, reference, float(u_prev))

        # --- deterministic test-only failure injection ---
        force = self.force_fail
        try:
            if force == "error":
                raise RuntimeError("forced solver error (test hook)")
            if force == "infeasible":
                # Construct a genuinely infeasible QP: force |u0|>a_max.
                qp = self._inject_infeasible(qp)
            if force == "nonfinite":
                # Hand OSQP a non-finite linear cost vector.
                bad_q = qp.q.copy()
                bad_q[0] = np.nan
                qp = QPMatrices(qp.P, bad_q, qp.A, qp.lower, qp.upper,
                                qp.L, qp.Phi, qp.Gamma)

            solver = self._setup_solver(qp, force_nonoptimal=(force == "nonoptimal"))

            if force == "timeout":
                # Deterministically exercise the post-solve timeout branch
                # (the small QPs here cannot naturally exceed the wall-clock
                # limit; a natural timeout is covered separately in tests
                # with a large horizon and tiny time_limit).
                result = solver.solve()
                return fail(FallbackReason.SOLVER_TIMEOUT,
                            "solver exceeded time_limit (forced for test)",
                            str(result.info.status))

            result = solver.solve()
        except RuntimeError as exc:
            if force == "error":
                return fail(FallbackReason.SOLVER_ERROR, str(exc))
            raise

        elapsed = time.perf_counter() - start
        status = str(result.info.status)

        if "time limit reached" in status:
            return fail(FallbackReason.SOLVER_TIMEOUT,
                        f"OSQP did not finish within "
                        f"{self.config.osqp_time_limit} s "
                        f"(status: {status!r})", status)
        if status in ("primal infeasible", "dual infeasible"):
            return fail(FallbackReason.SOLVER_INFEASIBLE,
                        f"OSQP reported {status}", status)
        if status != "solved":
            return fail(FallbackReason.SOLVER_NON_OPTIMAL,
                        f"OSQP ended with non-optimal status: {status!r}",
                        status)

        z = np.asarray(result.x, dtype=float)
        if not np.all(np.isfinite(z)):
            return fail(FallbackReason.SOLUTION_NONFINITE,
                        "OSQP solution contains NaN or Inf", status)

        ok, residuals = self._verify_solution(qp, z, x0, float(u_prev))
        if not ok:
            return fail(
                FallbackReason.CONSTRAINT_RESIDUAL,
                "post-solve verification failed: "
                + ", ".join(f"{k}={v:.3e}" for k, v in residuals.items()),
                status,
            )

        u_seq, states = self.builder.reconstruct(qp, x0, z, float(u_prev))
        u0 = float(np.clip(u_seq[0], -self.config.a_max, self.config.a_max))

        return MPCResult(
            status="ok",
            control=u0,
            fallback=False,
            reason=FallbackReason.NONE,
            reason_detail="optimal solution verified against hard constraints",
            solve_time_s=elapsed,
            osqp_status=status,
            objective=float(result.info.obj_val),
            control_sequence=[float(v) for v in u_seq],
            predicted_states=[[float(s[0]), float(s[1])] for s in states],
            residuals=residuals,
        )

    def _inject_infeasible(self, qp: QPMatrices) -> QPMatrices:
        """Pin Du_0 to two incompatible values via extra A rows."""
        import scipy.sparse as sp

        n = qp.A.shape[1]
        row = sp.csc_matrix(([1.0], ([0], [0])), shape=(1, n))
        A_new = sp.vstack([qp.A, row, row], format="csc")
        lower = np.concatenate([qp.lower, [100.0], [-100.0]])
        upper = np.concatenate([qp.upper, [101.0], [-99.0]])
        return QPMatrices(qp.P, qp.q, A_new, lower, upper,
                          qp.L, qp.Phi, qp.Gamma)
