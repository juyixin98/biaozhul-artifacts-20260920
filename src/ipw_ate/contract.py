"""Statistical contract: what is estimated, for whom, and how it is judged.

This module is pure data + declared constants. It contains no numerical
estimation logic, so the contract can be read (and tested) independently of
any estimator implementation.

Estimands
~~~~~~~~~
- ``ATE``  Average Treatment effect on the *combined* treated+control
           population. Stable (Hajek) weights normalized within arm.
- ``ATT``  Average Treatment effect on the *TREATED* population. The treated
           arm is unweighted (weight 1); the control arm is weighted by
           e(X)/(1-e(X)) and normalized over controls.

Weighting
~~~~~~~~~
Stable weights are used (so the answer does not depend on the overall
treatment probability scale). Truncation is FIXED and declared here, never a
silent adaptive floor on the denominator:

    ATE treated weight:   min(1/e(X), 1/LOWER)
    ATE control weight:   min(1/(1-e(X)), 1/LOWER)

A score exactly 0 or 1 (or non-finite) is an error, not a denominator
replacement. See :class:`ipw_ate.errors.PropensityScoreError`.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Final

import numpy as np

# --- Fixed, declared numerical contract ------------------------------------

#: Fixed weight-truncation quantile-free floor on the *probability* scale.
#: Weights are capped at 1/TRIM_LOWER and 1/(1-TRIM_UPPER). Versioned because
#: changing it changes the estimand; pinned so reruns are reproducible.
TRIM_LOWER: Final[float] = 0.01
TRIM_UPPER: Final[float] = 0.99
TRIM_VERSION: Final[str] = "fixed_ps_0.01_0.99_v1"

#: Minimum effective sample size (per arm) below which the result is rejected.
MIN_ESS_PER_ARM: Final[float] = 10.0

#: Propensity scores outside [EPS_WARN, 1-EPS_WARN] are flagged as "extreme"
#: in diagnostics (informational; does not by itself reject).
EPS_EXTREME: Final[float] = 1e-3


class Estimand(str, enum.Enum):
    """Target population for the effect."""

    ATE = "ATE"
    ATT = "ATT"


class Decision(str, enum.Enum):
    """Diagnostic verdict."""

    ACCEPT = "accept"
    REJECT = "reject"
    INCONCLUSIVE = "inconclusive"


class RejectReason(str, enum.Enum):
    """Machine-readable reasons a run is rejected or cannot be judged."""

    SCORE_AT_BOUNDARY = "score_at_boundary"
    MODEL_SEPARATION = "model_separation"
    NO_OVERLAP_CELL = "no_overlap_cell"
    LOW_ESS = "low_effective_sample_size"
    EXTREME_WEIGHTS = "extreme_weights"
    EMPTY_ARM = "empty_arm"
    INSUFFICIENT_DATA = "insufficient_data"
    UNDEFINED_WEIGHT = "undefined_weight"
    COVARIATE_IMBALANCE = "covariate_imbalance"


@dataclass(frozen=True)
class IPWConfig:
    """Declared analysis configuration (immutable).

    ``n_splits`` cross-fitting folds are used. The propensity model is trained
    on ``n_splits-1`` folds and predicts the held-out fold; every unit's
    score therefore comes from a model that did not see it.
    """

    estimand: Estimand = Estimand.ATE
    n_splits: int = 5
    trim_lower: float = TRIM_LOWER
    trim_upper: float = TRIM_UPPER
    trim_version: str = TRIM_VERSION
    min_ess_per_arm: float = MIN_ESS_PER_ARM
    random_seed: int = 20260927
    max_score: float = 1e-6  # |score| deviation that still counts as exact 0/1

    def __post_init__(self) -> None:
        if not 2 <= self.n_splits <= 50:
            raise ValueError("n_splits must be in [2, 50]")
        if not 0.0 < self.trim_lower < self.trim_upper < 1.0:
            raise ValueError("require 0 < trim_lower < trim_upper < 1")
        if self.estimand not in Estimand:
            raise ValueError(f"unknown estimand {self.estimand!r}")


@dataclass(frozen=True)
class ObservationSet:
    """Validated analysis data: binary treatment, outcome, covariates."""

    treatment: np.ndarray  # shape (n,) int in {0,1}
    outcome: np.ndarray  # shape (n,) float
    covariates: np.ndarray  # shape (n, p) float
    feature_names: tuple[str, ...] = field(default_factory=tuple)

    @property
    def n(self) -> int:
        return int(self.treatment.shape[0])

    @property
    def p(self) -> int:
        return int(self.covariates.shape[1])


@dataclass(frozen=True)
class ArmEvidence:
    """Per-arm weight/overlap evidence."""

    arm: int
    n: int
    weight_sum: float
    ess: float
    max_weight: float
    mean_propensity: float


@dataclass(frozen=True)
class DiagnosticResult:
    """Full evidence packet and the decision derived from it."""

    decision: Decision
    reasons: tuple[str, ...]
    request_id: str
    n: int
    n_treated: int
    n_control: int
    n_splits: int
    estimand: str
    trim_version: str
    score_min: float
    score_max: float
    n_extreme_scores: int
    n_boundary_scores: int
    prop_std: float
    max_weight: float
    ess_treated: float
    ess_control: float
    single_arm_cells: int
    max_balance_z: float
    balance_basis: str
    folds: tuple[tuple[int, ...], ...]
    arms: tuple[ArmEvidence, ...]
    assumptions: tuple[str, ...]
    message: str


@dataclass(frozen=True)
class EstimateResult:
    """Point estimate, uncertainty and the diagnostic packet that gates it."""

    estimand: str
    estimate: float
    std_error: float
    ci_lower: float
    ci_upper: float
    ci_level: float
    weights: np.ndarray  # shape (n,) stable, trimmed, normalized per arm
    propensity: np.ndarray  # shape (n,) cross-fitted scores
    fold_ids: np.ndarray  # shape (n,)
    diagnostic: DiagnosticResult
    request_id: str
