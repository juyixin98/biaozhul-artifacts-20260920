"""Configuration for the Krylov expv integrator and the service layer.

All numerical policy knobs live here so that time segmentation and restart
rules are fixed, explicit, and testable -- nothing is tuned ad hoc inside
the computational kernel.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ExpvConfig:
    """Numerical policy for a single exp(t*A) @ v evaluation.

    Attributes:
        m_max: maximum Krylov subspace dimension per step (basis restart
            happens implicitly at every accepted time step).
        tol: target relative error tolerance for the whole interval [0, t].
        max_steps: hard cap on accepted time steps (== number of basis
            rebuilds / restarts).  Exceeding it yields a *not converged*
            result, never a silent answer.
        max_halvings: cap on step-size halvings within one step before the
            step is rejected as underflowed.
        memory_budget_bytes: budget covering Krylov basis storage
            ((m_max + 1) * n * 8 bytes), the Hessenberg matrix, and work
            vectors.  Requests that would exceed the budget are rejected
            up front with category ``memory_budget_exceeded``.
        step_shrink: deterministic factor applied when a step is rejected.
        step_grow: deterministic factor applied to the next trial step when
            the current estimate is far below the local tolerance.
        grow_threshold: fraction of the local tolerance below which the
            next step size is grown.
        acceptance_safety: fixed safety margin on step acceptance -- a step
            is accepted only when its estimate is below
            ``acceptance_safety * tol_local``, because the phi_1 estimate
            captures only the leading neglected term.
        breakdown_tol: Arnoldi happy-breakdown threshold on h_{j+1,j},
            relative to the norm of the incoming vector.
    """

    m_max: int = 30
    tol: float = 1.0e-8
    max_steps: int = 512
    max_halvings: int = 60
    memory_budget_bytes: int = 256 * 1024**2
    step_shrink: float = 0.5
    step_grow: float = 2.0
    grow_threshold: float = 0.1
    acceptance_safety: float = 0.25
    breakdown_tol: float = 1.0e-14

    def basis_memory_bytes(self, n: int) -> int:
        """Bytes needed to store the Krylov basis and Hessenberg matrix."""
        basis = (self.m_max + 1) * n * 8
        hessenberg = (self.m_max + 1) * self.m_max * 8
        work_vectors = 4 * n * 8
        return basis + hessenberg + work_vectors

    @classmethod
    def from_env(cls) -> "ExpvConfig":
        """Build a config from KRYLOV_EXPV_* environment variables."""
        kwargs: dict[str, object] = {}
        for field in ("m_max", "max_steps", "max_halvings", "memory_budget_bytes"):
            env = os.environ.get(f"KRYLOV_EXPV_{field.upper()}")
            if env is not None:
                kwargs[field] = int(env)
        env_tol = os.environ.get("KRYLOV_EXPV_TOL")
        if env_tol is not None:
            kwargs["tol"] = float(env_tol)
        return cls(**kwargs)  # type: ignore[arg-type]
