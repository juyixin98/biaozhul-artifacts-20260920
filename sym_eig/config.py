"""Runtime configuration.

All limits and tolerances are explicit and overridable per request or via
environment variables. Nothing numerical is hardcoded inside the kernel.
"""

from dataclasses import dataclass, replace
import os

# Machine epsilon for IEEE-754 binary64, used as the default QR deflation
# tolerance (kept as a named constant rather than obtained magically).
FLOAT64_EPS = 2.220446049250313e-16


@dataclass(frozen=True)
class Settings:
    """Service-wide limits and tolerances.

    Attributes:
        max_n: Maximum accepted matrix dimension (size budget).
        max_iters: Default per-block implicit-QR sweep budget (iteration
            budget). A request may lower it; it may never raise it above
            ``hard_max_iters``.
        hard_max_iters: Absolute ceiling for per-request iteration budgets.
        symmetry_rtol / symmetry_atol: Relative/absolute symmetry check,
            evaluated as ``max|A - A^T| <= atol + rtol * max|A|``.
        convergence_tol: QR off-diagonal deflation tolerance relative to the
            neighbouring diagonal entries.
        residual_rtol: Gate threshold for the relative eigen residual and the
            per-eigenvalue residual.
        orthogonality_tol: Gate threshold for ``||V^T V - I||_F``.
        reconstruction_rtol: Gate threshold for ``||A - V diag(w) V^T||_F``
            relative to ``||A||_F``.
        cluster_rtol: Eigenvalues whose relative gap is below this are treated
            as one (near-)degenerate cluster and compared by *subspace*, never
            eigenvector by eigenvector.
        reference_dps: mpmath decimal digits for the independent oracle.
        reference_max_n: Largest dimension for which the mpmath oracle runs
            (it is dense arbitrary-precision and deliberately bounded).
    """

    max_n: int = 256
    max_iters: int = 30
    hard_max_iters: int = 1000
    symmetry_rtol: float = 1e-10
    symmetry_atol: float = 1e-12
    convergence_tol: float = FLOAT64_EPS
    residual_rtol: float = 1e-9
    orthogonality_tol: float = 1e-9
    reconstruction_rtol: float = 1e-9
    cluster_rtol: float = 1e-8
    reference_dps: int = 50
    reference_max_n: int = 24

    @classmethod
    def from_env(cls) -> "Settings":
        """Build settings from ``SYMEIG_*`` environment variables."""
        base: Settings = cls()
        overrides = {}
        int_fields = {"max_n", "max_iters", "hard_max_iters",
                      "reference_dps", "reference_max_n"}
        float_fields = {
            "symmetry_rtol", "symmetry_atol", "convergence_tol",
            "residual_rtol", "orthogonality_tol", "reconstruction_rtol",
            "cluster_rtol",
        }
        for name in int_fields:
            raw = os.environ.get(f"SYMEIG_{name.upper()}")
            if raw is not None:
                overrides[name] = int(raw)
        for name in float_fields:
            raw = os.environ.get(f"SYMEIG_{name.upper()}")
            if raw is not None:
                overrides[name] = float(raw)
        return replace(base, **overrides) if overrides else base
