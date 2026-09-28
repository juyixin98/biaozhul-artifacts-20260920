"""Statistical contract: the declared, serializable agreement behind every result.

The contract object is immutable and fully recorded with each run so that a
result can never be read without knowing:

* the target population (ATE / ATT / ATU),
* the exact weight definition (stabilized vs Horvitz-Thompson),
* the cross-fitting scheme (K, stratification, seed),
* the propensity model family and regularization,
* the FIXED clipping profile (or its absence),
* the identification assumptions the estimate is conditional on.

Nothing here estimates anything; this module defines *what is claimed*.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Literal

from .config import AppConfig


class Verdict(str, Enum):
    ACCEPT = "accept"
    WARN = "warn"
    REJECT = "reject"
    INCONCLUSIVE = "inconclusive"


Severity = Literal["info", "warn", "reject"]


@dataclass(frozen=True)
class Assumptions:
    """Identification assumptions. Outputs are conditional on all of these."""

    consistency: str = (
        "Observed outcome equals the potential outcome under the observed treatment."
    )
    conditional_exchangeability: str = (
        "Y(a) independent of A given X (no unmeasured confounding). "
        "This is NOT testable from the data and cannot be proven by the estimate."
    )
    positivity: str = (
        "0 < P(A=1|X) < 1 for every unit in the target population; "
        "overlap diagnostics provide supporting but incomplete evidence."
    )
    sutta_note: str = (
        "SUTVA: no interference between units and a single well-defined treatment version."
    )


@dataclass(frozen=True)
class StatisticalContract:
    estimand: str
    target_population: str
    weight_type: str
    weight_definition: dict[str, str]
    crossfit_n_splits: int
    crossfit_stratified: bool
    crossfit_seed: int
    model_family: str
    model_ridge_lambda: float
    model_max_iter: int
    model_tol: float
    clipping_enabled: bool
    clipping_profile: str
    clipping_lower: float
    clipping_upper: float
    positivity_eps: float
    estimand_note: str
    assumptions: dict[str, str]

    @classmethod
    def from_config(cls, config: AppConfig) -> "StatisticalContract":
        target = {
            "ate": "ATE targets the FULL observed population E[Y(1)-Y(0)] over the covariate distribution of all units.",
            "att": "ATT targets the TREATED subpopulation E[Y(1)-Y(0) | A=1]; treated units receive weight 1.",
            "atu": "ATU targets the UNTREATED subpopulation E[Y(1)-Y(0) | A=0]; untreated units receive weight 1.",
        }[config.estimand]
        if config.weight_type == "stabilized":
            definition = {
                "ate": "w_i = A_i * e_i/p_hat / e_i + (1-A_i) * (1-e_i)/(1-p_hat)/(1-e_i); "
                "numerators are marginal treatment proportions e_i=P(A=1) (and 1-e_i), "
                "denominators are out-of-fold p_hat(X_i)=P(A=1|X_i).",
                "att": "w_i = A_i + (1-A_i) * p_hat(X_i)/(1-p_hat(X_i)) * (1-e)/e, "
                "e = P(A=1) over ALL units; target is the treated.",
                "atu": "w_i = A_i * (1-p_hat(X_i))/p_hat(X_i) * e/(1-e) + (1-A_i), "
                "e = P(A=1) over ALL units; target is the untreated.",
            }[config.estimand]
        else:
            definition = {
                "ate": "w_i = A_i/p_hat(X_i) + (1-A_i)/(1-p_hat(X_i)) (Horvitz-Thompson).",
                "att": "w_i = A_i + (1-A_i) * p_hat(X_i)/(1-p_hat(X_i)) (Horvitz-Thompson).",
                "atu": "w_i = A_i * (1-p_hat(X_i))/p_hat(X_i) + (1-A_i) (Horvitz-Thompson).",
            }[config.estimand]
        clip = config.weights.clipping
        a = Assumptions()
        return cls(
            estimand=config.estimand,
            target_population=target,
            weight_type=config.weight_type,
            weight_definition={"formula": definition},
            crossfit_n_splits=config.crossfit.n_splits,
            crossfit_stratified=config.crossfit.stratify,
            crossfit_seed=config.crossfit.seed,
            model_family=config.model.family,
            model_ridge_lambda=config.model.ridge_lambda,
            model_max_iter=config.model.max_iter,
            model_tol=config.model.tol,
            clipping_enabled=clip.enabled,
            clipping_profile=clip.profile if clip.enabled else "none",
            clipping_lower=clip.lower if clip.enabled else 0.0,
            clipping_upper=clip.upper if clip.enabled else 1.0,
            positivity_eps=config.weights.positivity_eps,
            estimand_note=(
                "Clipping is enabled: weights use the FIXED bounds [%.3f, %.3f]. "
                "The resulting estimand is redefined over the trimmed population and "
                "is NOT the unclipped %s."
                % (clip.lower, clip.upper, config.estimand.upper())
                if clip.enabled
                else "No clipping: estimand is the untrimmed %s." % config.estimand.upper()
            ),
            assumptions=asdict(a),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FoldRecord:
    fold: int
    n_train: int
    n_eval: int
    n_train_treated: int
    n_train_untreated: int
    converged: bool
    n_iter: int


@dataclass(frozen=True)
class DiagnosticFinding:
    code: str
    severity: Severity
    status: str  # accept | warn | reject | inconclusive
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OverlapSummary:
    pscore_min: float
    pscore_max: float
    treated_q05: float
    treated_q95: float
    untreated_q05: float
    untreated_q95: float
    treated_n: int
    untreated_n: int
    n_clipped: int
    n_near_boundary: int


@dataclass(frozen=True)
class WeightSummary:
    n: int
    treated_ess: float
    untreated_ess: float
    treated_ess_fraction: float
    untreated_ess_fraction: float
    overall_ess: float
    overall_ess_fraction: float
    max_weight: float
    mean_weight_treated: float
    mean_weight_untreated: float
    weight_sum_treated: float
    weight_sum_untreated: float
    # Hand-auditable raw ingredients (small fixtures only in practice):
    pscore_summary: dict[str, float]


@dataclass(frozen=True)
class Estimate:
    point: float
    se: float
    ci_level: float
    ci_lower: float
    ci_upper: float
    mu1: float
    mu0: float
    influence_used: bool


@dataclass(frozen=True)
class EstimationResult:
    request_id: str
    contract: StatisticalContract
    verdict: str
    estimate: Estimate
    weights: WeightSummary
    overlap: OverlapSummary
    calibration: dict[str, Any]
    findings: list[DiagnosticFinding]
    folds: list[FoldRecord]
    n_observations: int
    n_covariates: int
    warnings: list[str]
    causal_disclaimer: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "verdict": self.verdict,
            "estimate": asdict(self.estimate),
            "weights": asdict(self.weights),
            "overlap": asdict(self.overlap),
            "calibration": self.calibration,
            "findings": [asdict(f) for f in self.findings],
            "folds": [asdict(f) for f in self.folds],
            "n_observations": self.n_observations,
            "n_covariates": self.n_covariates,
            "warnings": self.warnings,
            "contract": self.contract.to_dict(),
            "causal_disclaimer": self.causal_disclaimer,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True, allow_nan=False)


CAUSAL_DISCLAIMER = (
    "This estimate is a conditional association under the declared contract. "
    "A causal interpretation requires unconfoundedness (no unmeasured confounding), "
    "positivity and consistency, which the data cannot establish. Overlap diagnostics "
    "can reject or flag weak support but never prove the assumptions. "
    "This number is not causal proof."
)
