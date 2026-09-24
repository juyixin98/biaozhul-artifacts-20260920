"""Bounded linear MPC for the discretised double integrator.

Decision variables (N = horizon):

    z = [x_1 .. x_N | u_0 .. u_{N-1}]      x_k in R^2,  u_k in R

Cost (all weight matrices are built from the model + configured weights):

    sum_{k=1..N-1} (x_k - r_k)' Q (x_k - r_k)
        + (x_N - r_N)' Q_f (x_N - r_N)           Q_f from DARE(Q, R)
        + sum u_k' R_u u_k
        + sum_{k=0..N-1} (u_k - u_{k-1})' R_du (u_k - u_{k-1})

u_{-1} is the supplied ``u_prev`` (control-change regularisation around the
*currently applied* input, never around an unverified solution).

Constraints:

    dynamics:  x_1 = A x0 + B u0 ; x_{k+1} = A x_k + B u_k   (equalities)
    velocity:  -v_max <= v_k <= v_max      k = 1..N
    accel:     -u_max <= u_k <= u_max      k = 0..N-1

The returned control is applied **only if the solution is verified**
(dynamics, state and input residuals below tolerance).  On solver failure,
timeout/incomplete solve, infeasibility, or failed verification a conservative
fallback is returned: the brake that drives the current velocity to zero in one
sample, clipped to the input bounds.  A stale/last solution is never returned.
"""

from __future__ import annotations

import dataclasses
import enum
import time

import numpy as np
import osqp
import scipy.sparse as sp
from scipy.linalg import solve_discrete_are

from .model import DoubleIntegrator


class MpcStatus(str, enum.Enum):
    SOLVED = "solved"
    INFEASIBLE_INITIAL_STATE = "infeasible_initial_state"
    SOLVER_INFEASIBLE = "solver_infeasible"
    SOLVER_TIMEOUT = "solver_timeout"
    SOLVER_ERROR = "solver_error"
    SOLVER_INCOMPLETE = "solver_incomplete"
    VERIFICATION_FAILED = "verification_failed"


_FALLBACK_STATUSES = frozenset(
    {
        MpcStatus.INFEASIBLE_INITIAL_STATE,
        MpcStatus.SOLVER_INFEASIBLE,
        MpcStatus.SOLVER_TIMEOUT,
        MpcStatus.SOLVER_ERROR,
        MpcStatus.SOLVER_INCOMPLETE,
        MpcStatus.VERIFICATION_FAILED,
    }
)


@dataclasses.dataclass(frozen=True)
class MpcConfig:
    horizon: int = 20
    dt: float = 0.1
    v_max: float = 2.0
    u_max: float = 1.0
    q_position: float = 20.0
    q_velocity: float = 2.0
    r_input: float = 1.0e-3
    r_rate: float = 0.5
    time_limit: float = 0.05
    max_iter: int = 20000
    eps_abs: float = 1.0e-7
    eps_rel: float = 1.0e-7
    polish: bool = True
    verify_tol: float = 1.0e-4

    def __post_init__(self) -> None:
        if self.horizon < 1:
            raise ValueError("horizon must be >= 1")
        if self.dt <= 0:
            raise ValueError("dt must be positive")
        if self.v_max <= 0 or self.u_max <= 0:
            raise ValueError("v_max and u_max must be positive")
        for name in ("q_position", "q_velocity", "r_input", "r_rate"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.q_position + self.q_velocity <= 0:
            raise ValueError("Q must be non-zero")
        if self.time_limit <= 0:
            raise ValueError("time_limit must be positive")
        if self.verify_tol <= 0:
            raise ValueError("verify_tol must be positive")


@dataclasses.dataclass
class Residuals:
    """Infinity-norm constraint residuals of the *verified* solution."""

    dynamics: float = 0.0
    state: float = 0.0  # velocity-limit violation (>0 means violated)
    input: float = 0.0  # acceleration-limit violation (>0 means violated)

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class MpcResult:
    status: MpcStatus
    control: float
    fallback: bool
    reason: str
    solve_time_s: float
    iterations: int
    osqp_status: str | None
    residuals: Residuals
    predicted_states: list[list[float]]
    predicted_controls: list[float]

    def as_dict(self) -> dict:
        return {
            "status": self.status.value,
            "control": self.control,
            "fallback": self.fallback,
            "reason": self.reason,
            "solve_time_s": self.solve_time_s,
            "iterations": self.iterations,
            "osqp_status": self.osqp_status,
            "residuals": self.residuals.as_dict(),
            "predicted_states": self.predicted_states,
            "predicted_controls": self.predicted_controls,
        }


class BoundedMPC:
    """OSQP-backed bounded MPC built entirely from :class:`DoubleIntegrator`."""

    def __init__(self, config: MpcConfig | None = None):
        self.cfg = config or MpcConfig()
        self.model = DoubleIntegrator(self.cfg.dt)
        self.N = self.cfg.horizon
        self.n, self.m = self.model.n, self.model.m
        self.A, self.B = self.model.A, self.model.B
        self.nz = self.N * (self.n + self.m)
        self.nx_ = self.N * self.n
        self.nu_ = self.N * self.m
        self.Q = np.diag([self.cfg.q_position, self.cfg.q_velocity]).astype(float)
        self.Qf = solve_discrete_are(
            self.A, self.B, self.Q, np.array([[self.cfg.r_rate + self.cfg.r_input]])
        )
        self.Ru = np.array([[self.cfg.r_input]], dtype=float)
        self.Rdu = np.array([[self.cfg.r_rate]], dtype=float)
        self._P, self._A_con, self._bounds_template = self._build_matrices()

    # ------------------------------------------------------------------ build
    def _build_matrices(self) -> tuple[sp.csc_matrix, sp.csc_matrix, np.ndarray]:
        """Construct OSQP Hessian ``P``, constraint matrix ``A`` and bounds.

        Everything derives from the plant's ``A, B`` and configured
        weights/limits — no scenario-specific numbers are hard-coded.
        Constraint rows, in order:
          [0, N*n)          dynamics equalities (first n carry A x0)
          [N*n, N*n+N)      velocity bounds
          [N*n+N, N*n+2N)   input bounds
        """
        N, n, m = self.N, self.n, self.m
        rows, cols, data = [], [], []

        def add_triu(k0: int, M: np.ndarray) -> None:
            Mr = sp.triu(sp.csc_matrix(M), format="coo")
            rows.extend(Mr.row + k0)
            cols.extend(Mr.col + k0)
            data.extend(Mr.data)

        def add_scalar(i: int, j: int, v: float) -> None:
            rows.append(i)
            cols.append(j)
            data.append(v)

        # ---- P (upper triangular; OSQP objective has the factor 1/2)
        for k in range(1, N):
            add_triu((k - 1) * n, 2.0 * self.Q)
        add_triu((N - 1) * n, 2.0 * self.Qf)

        for j in range(N):
            # rate links touching u_j: predecessor link (to u_{j-1} or u_prev)
            # plus successor link (to u_{j+1}) when it exists
            n_links = 1 + (1 if j < N - 1 else 0)
            add_scalar(self.nx_ + j, self.nx_ + j,
                       2.0 * self.Ru[0, 0] + 2.0 * self.Rdu[0, 0] * n_links)
        for j in range(N - 1):
            add_scalar(self.nx_ + j, self.nx_ + j + 1, -2.0 * self.Rdu[0, 0])

        P = sp.csc_matrix(
            (np.asarray(data, dtype=float), (rows, cols)), shape=(self.nz, self.nz)
        )

        # ---- constraints
        crow, zcol, vals = [], [], []
        lo, hi = [], []
        row = 0

        def coeff(r: int, ci: int, v: float) -> None:
            if v != 0.0:
                crow.append(r)
                zcol.append(ci)
                vals.append(float(v))

        # dynamics equalities:
        #   k=0:    I x_1 - B u_0 = A x0      (bound filled at solve time)
        #   k>=1:   I x_{k+1} - A x_k - B u_k = 0
        for k in range(N):
            for i in range(n):
                for j in range(n):
                    coeff(row + i, k * n + j, 1.0 if i == j else 0.0)
                if k >= 1:
                    for j in range(n):
                        coeff(row + i, (k - 1) * n + j, -self.A[i, j])
                for j in range(m):
                    coeff(row + i, self.nx_ + k * m + j, -self.B[i, j])
                lo.append(0.0)
                hi.append(0.0)
            row += n

        # velocity bounds on v_k (state component index 1), k=1..N
        for k in range(1, N + 1):
            coeff(row, (k - 1) * n + 1, 1.0)
            lo.append(-self.cfg.v_max)
            hi.append(self.cfg.v_max)
            row += 1

        # input bounds u_j
        for j in range(N):
            coeff(row, self.nx_ + j, 1.0)
            lo.append(-self.cfg.u_max)
            hi.append(self.cfg.u_max)
            row += 1

        A_con = sp.csc_matrix(
            (np.asarray(vals, dtype=float), (crow, zcol)), shape=(row, self.nz)
        )
        bounds = np.vstack([np.asarray(lo, dtype=float),
                            np.asarray(hi, dtype=float)]).T
        return P, A_con, bounds

    # --------------------------------------------------------------- fallback
    def fallback_control(self, velocity: float) -> float:
        """Conservative brake: command -v/dt (stop next sample), clipped."""
        u_brake = -float(velocity) / self.cfg.dt
        return float(np.clip(u_brake, -self.cfg.u_max, self.cfg.u_max))

    def _make_result(
        self,
        status: MpcStatus,
        reason: str,
        control: float,
        solve_time_s: float,
        iterations: int,
        osqp_status: str | None,
        states: np.ndarray | None = None,
        controls: np.ndarray | None = None,
        residuals: Residuals | None = None,
    ) -> MpcResult:
        return MpcResult(
            status=status,
            control=float(control),
            fallback=status in _FALLBACK_STATUSES,
            reason=reason,
            solve_time_s=solve_time_s,
            iterations=iterations,
            osqp_status=osqp_status,
            residuals=residuals or Residuals(),
            predicted_states=(states if states is not None else np.zeros((0, self.n))).tolist(),
            predicted_controls=(controls if controls is not None else np.zeros(0)).tolist(),
        )

    # ------------------------------------------------------------------- solve
    def solve(
        self,
        x0: np.ndarray | list[float],
        reference: np.ndarray | list[float],
        u_prev: float | np.ndarray | list[float] = 0.0,
    ) -> MpcResult:
        """Solve one MPC problem.

        ``reference`` holds position targets for k=1..N; shorter sequences are
        extended by holding the last target.
        """
        t0 = time.perf_counter()
        x0 = np.asarray(x0, dtype=float).reshape(self.n)
        ref = np.asarray(reference, dtype=float).reshape(-1)
        if ref.size == 0:
            raise ValueError("reference must contain at least one target")
        if ref.size < self.N:
            ref = np.concatenate([ref, np.full(self.N - ref.size, ref[-1])])
        ref = ref[: self.N]
        u_prev = float(np.asarray(u_prev, dtype=float).reshape(1)[0])

        # 1) upfront feasibility of the *initial state* (velocity bound)
        if abs(x0[1]) > self.cfg.v_max + 1e-12:
            return self._make_result(
                MpcStatus.INFEASIBLE_INITIAL_STATE,
                f"initial velocity {x0[1]:.6g} exceeds v_max={self.cfg.v_max}",
                self.fallback_control(x0[1]),
                time.perf_counter() - t0,
                0,
                None,
            )

        # 2) linear cost from reference + u_prev
        q = np.zeros(self.nz)
        for k in range(1, self.N):
            q[(k - 1) * self.n : k * self.n] = -2.0 * self.Q @ np.array(
                [ref[k - 1], 0.0]
            )
        q[(self.N - 1) * self.n : self.N * self.n] = -2.0 * self.Qf @ np.array(
            [ref[-1], 0.0]
        )
        q[self.nx_] = -2.0 * self.Rdu[0, 0] * u_prev

        # 3) constraint bounds: first dynamics rows carry A x0
        bnd = self._bounds_template.copy()
        bnd[: self.n, :] = (self.A @ x0)[:, None]

        # 4) solve (fresh instance per call -> deterministic, no stale state)
        try:
            solver = osqp.OSQP()
            setup_kwargs = dict(
                verbose=False,
                eps_abs=self.cfg.eps_abs,
                eps_rel=self.cfg.eps_rel,
                max_iter=self.cfg.max_iter,
                time_limit=self.cfg.time_limit,
            )
            try:
                solver.setup(self._P, q, self._A_con, bnd[:, 0], bnd[:, 1],
                             polishing=self.cfg.polish, **setup_kwargs)
            except TypeError:  # older OSQP used `polish`
                solver.setup(self._P, q, self._A_con, bnd[:, 0], bnd[:, 1],
                             polish=self.cfg.polish, **setup_kwargs)
            res = solver.solve()
        except Exception as exc:  # solver failure must never propagate raw
            return self._make_result(
                MpcStatus.SOLVER_ERROR,
                f"OSQP raised: {type(exc).__name__}: {exc}",
                self.fallback_control(x0[1]),
                time.perf_counter() - t0,
                0,
                None,
            )

        elapsed = time.perf_counter() - t0
        osqp_status = str(res.info.status)
        iters = int(getattr(res.info, "iter", 0) or 0)

        if osqp_status != "solved":
            if "infeasible" in osqp_status:
                status, reason = (
                    MpcStatus.SOLVER_INFEASIBLE,
                    f"OSQP reported '{osqp_status}'; no feasible input sequence",
                )
            elif "time limit" in osqp_status:
                status, reason = (
                    MpcStatus.SOLVER_TIMEOUT,
                    f"OSQP hit time limit ({self.cfg.time_limit}s) before convergence",
                )
            else:
                status, reason = (
                    MpcStatus.SOLVER_INCOMPLETE,
                    f"OSQP ended non-optimal with status '{osqp_status}'",
                )
            return self._make_result(
                status, reason, self.fallback_control(x0[1]),
                elapsed, iters, osqp_status,
            )

        # 5) verify the candidate independently before it may leave the module
        z = np.asarray(res.x).reshape(-1)
        X = z[: self.nx_].reshape(self.N, self.n)
        U = z[self.nx_ :].reshape(self.N, self.m)

        residuals = self._verify(x0, X, U)
        tol = self.cfg.verify_tol
        if max(residuals.dynamics, residuals.state, residuals.input) > tol:
            return self._make_result(
                MpcStatus.VERIFICATION_FAILED,
                "verified solution violates constraints "
                f"(dyn={residuals.dynamics:.2e}, state={residuals.state:.2e}, "
                f"input={residuals.input:.2e} > tol={tol:.0e})",
                self.fallback_control(x0[1]),
                elapsed, iters, osqp_status,
            )

        return self._make_result(
            MpcStatus.SOLVED, "optimal verified solution", U[0, 0],
            elapsed, iters, osqp_status,
            states=X, controls=U[:, 0], residuals=residuals,
        )

    # --------------------------------------------------------------- verify
    def _verify(self, x0: np.ndarray, X: np.ndarray, U: np.ndarray) -> Residuals:
        """Independent residual check — reuses no solver bookkeeping."""
        dyn = 0.0
        for k in range(self.N):
            xk = x0 if k == 0 else X[k - 1]
            dyn = max(dyn, float(np.max(np.abs(self.A @ xk + self.B @ U[k] - X[k]))))
        return Residuals(
            dynamics=dyn,
            state=float(max(0.0, np.max(np.abs(X[:, 1]) - self.cfg.v_max))),
            input=float(max(0.0, np.max(np.abs(U[:, 0]) - self.cfg.u_max))),
        )

    # ----------------------------------------------------------------- expose
    def matrices(self) -> dict:
        """Return model/built matrices (for auditing and tests)."""
        return {
            "A": self.A.tolist(),
            "B": self.B.tolist(),
            "Q": self.Q.tolist(),
            "Qf": self.Qf.tolist(),
            "Ru": self.Ru.tolist(),
            "Rdu": self.Rdu.tolist(),
            "P_nnz": int(self._P.nnz),
            "Acon_shape": list(self._A_con.shape),
        }
