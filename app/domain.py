"""Data contracts shared between layers.

Coefficient convention (single source of truth):
    p(z) = c[0] * z**n + c[1] * z**(n-1) + ... + c[n]
i.e. *descending* powers, c[0] is the leading coefficient, n = degree.
Every layer receives coefficients in this convention and must not reinterpret.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

import numpy as np

# Hard resource caps (defaults; the service may tighten but never loosen silently).
DEFAULT_MAX_DEGREE = 200
DEFAULT_MAX_ITER = 200
HARD_MAX_ITER = 10_000
DEFAULT_CONV_TOL = 1e-12
DEFAULT_CLUSTER_TOL = 1e-6
DEFAULT_PAIR_TOL = 1e-8


class SolveStatus(str, Enum):
    CONVERGED = "converged"
    PARTIAL = "partial"          # iteration budget exhausted; unconverged state preserved
    FAILED = "failed"            # kernel failure (also surfaced as COMPUTATION_FAILED)


class Method(str, Enum):
    COMPANION_ABERTH = "companion+aberth"  # companion eigenvalues, then Aberth polish
    COMPANION = "companion"                # companion eigenvalues only
    ABERTH = "aberth"                      # Aberth from Cauchy-bound circle guesses


@dataclass(frozen=True)
class SolveOptions:
    method: Method = Method.COMPANION_ABERTH
    max_iter: int = DEFAULT_MAX_ITER
    conv_tol: float = DEFAULT_CONV_TOL
    cluster_tol: float = DEFAULT_CLUSTER_TOL
    pair_tol: float = DEFAULT_PAIR_TOL
    max_degree: int = DEFAULT_MAX_DEGREE


@dataclass(frozen=True)
class NormalizedPolynomial:
    """Result of the input boundary: a clean, monic-scaled polynomial."""

    coeffs: np.ndarray          # complex128, descending powers, coeffs[0] != 0
    degree: int
    leading_dropped: int        # how many leading (near-)zero coefficients were stripped
    scale: float                # max |c_k| of the *raw* input, used for tolerances


@dataclass
class RootEstimate:
    root: complex
    converged: bool
    iterations: int
    residual_abs: float = 0.0
    residual_rel: float = 0.0
    cluster_id: Optional[int] = None
    error_bound: float = float("nan")


@dataclass
class Cluster:
    cluster_id: int
    member_indices: List[int]
    diameter: float


@dataclass
class Evidence:
    """Error evidence. The accuracy contract lives here:

    - per-root residuals are reported, but for clustered (near-multiple) roots
      small residuals are NOT accepted as proof of accuracy; the cluster
      diameter is reported as the honest forward-error bound instead.
    """

    max_residual_rel: float
    reconstruction_error: float
    vieta_max_deviation: float
    clusters: List[Cluster] = field(default_factory=list)
    accuracy_note: str = ""


@dataclass
class SolveReport:
    run_id: str
    status: SolveStatus
    degree: int
    roots: List[RootEstimate]          # in deterministic output order
    evidence: Evidence
    pairs: List[List[int]]             # conjugate pairs as index pairs into `roots`
    unpaired: List[int]
    pair_tol: float
    leading_dropped: int
    message: str
