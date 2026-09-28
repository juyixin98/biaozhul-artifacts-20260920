"""Statistical contract: model specification, validated data, result shapes.

Notation (consistent across the whole package)
----------------------------------------------
n = number of observations
y : (n,)   outcome
Y : (n,k)  endogenous regressors (treated as correlated with the error)
X : (n,m)  included exogenous regressors (controls; may be empty -> intercept)
Z : (n,L)  excluded instruments (affect Y, assumed excluded from y equation)

Structural equation:   y = Y @ beta + X @ gamma + e
First stage:           Y = X @ Pi_x + Z @ Pi_z + V
Order condition:       L >= k  (necessary, not sufficient)
Rank condition:        rank(Z' M_X Y after partialling X) = k  (tested)

The exclusion restriction Cov(Z, e) = 0 is an *economic assumption supplied
by the caller*; first-stage relevance is testable from data, exogeneity of
instruments can only be jointly probed when L > k (Sargan/Hansen) and is never
"proven" by a correlation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import numpy as np
from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# API-layer schemas
# ---------------------------------------------------------------------------

CovKind = Literal["homoskedastic", "robust"]


class ModelSpec(BaseModel):
    """Which columns play which role in the IV model."""

    dependent: str = Field(description="Outcome column name (y).")
    endogenous: list[str] = Field(min_length=1, description="Endogenous regressor column names (Y).")
    included_exogenous: list[str] = Field(
        default_factory=lambda: ["const"],
        description="Included exogenous controls (X). Use ['const'] for an intercept-only controls set.",
    )
    excluded_instruments: list[str] = Field(
        min_length=1, description="Excluded instrument column names (Z)."
    )

    @field_validator("endogenous", "excluded_instruments", "included_exogenous")
    @classmethod
    def _no_dupes(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("duplicate column name within a role list")
        return v


class EstimationOptions(BaseModel):
    covariance: CovKind = "homoskedastic"
    strict: bool = Field(
        default=False,
        description="If true, weak/under-identification rejects the request; "
        "otherwise they are returned as diagnostics on the estimate.",
    )
    add_intercept: bool = Field(
        default=True, description="Always include an intercept in X even if not named."
    )
    confidence_level: float = Field(default=0.95, gt=0.0, lt=1.0)
    bootstrap_reps: int = Field(default=0, ge=0, le=10_000)


class InstrumentValidityClaim(BaseModel):
    """Caller-supplied economic assumptions. Recorded, never inferred.

    The service refuses to *derive* exclusion restrictions from sample
    correlations; the caller must assert them explicitly, and they are echoed
    verbatim (attributed to the caller) in the result.
    """

    exclusion_restriction_asserted: bool = False
    rationale: str = Field(
        default="",
        max_length=2000,
        description="Free-text economic reasoning; stored and echoed, not parsed.",
    )


class EstimationRequest(BaseModel):
    request_id: str = Field(min_length=1, max_length=128)
    columns: dict[str, list[float]] = Field(description="Column name -> n observations.")
    spec: ModelSpec
    options: EstimationOptions = Field(default_factory=EstimationOptions)
    validity_claim: InstrumentValidityClaim = Field(default_factory=InstrumentValidityClaim)

    @field_validator("columns")
    @classmethod
    def _columns_nonempty(cls, v: dict[str, list[float]]) -> dict[str, list[float]]:
        if not v:
            raise ValueError("columns must not be empty")
        lengths = {len(c) for c in v.values()}
        if len(lengths) != 1:
            raise ValueError(f"all columns must share one length, got {sorted(lengths)}")
        return v


# ---------------------------------------------------------------------------
# Kernel-layer containers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EstimationData:
    """Validated numeric matrices. Constructed only by ``build_data``."""

    request_id: str
    y: np.ndarray  # (n,)
    Y: np.ndarray  # (n,k)
    X: np.ndarray  # (n,m)
    Z: np.ndarray  # (n,L)
    endog_names: tuple[str, ...]
    exog_names: tuple[str, ...]
    instrument_names: tuple[str, ...]

    @property
    def nobs(self) -> int:
        return int(self.y.shape[0])

    @property
    def n_endog(self) -> int:
        return self.Y.shape[1]

    @property
    def n_exog(self) -> int:
        return self.X.shape[1]

    @property
    def n_instruments(self) -> int:
        return self.Z.shape[1]

    def regressor_names(self) -> tuple[str, ...]:
        return self.endog_names + self.exog_names

    def to_log_state(self) -> dict[str, object]:
        return {
            "nobs": self.nobs,
            "k_endog": self.n_endog,
            "m_exog": self.n_exog,
            "L_instruments": self.n_instruments,
        }


@dataclass(frozen=True)
class CoefficientResult:
    name: str
    estimate: float
    std_error: float
    t_stat: float
    p_value: float
    ci_low: float
    ci_high: float
    std_error_bootstrap: float | None = None


@dataclass(frozen=True)
class FirstStageResult:
    """Per-endogenous-regressor first-stage relevance diagnostics."""

    endogenous_name: str
    partial_r2: float
    partial_r2_adjusted: float
    f_statistic: float
    f_p_value: float
    effective_f_statistic: float  # Sanderson-Windmeijer multivariate F
    eigen_min_statistic: float  # Cragg-Donald / Angrist-Pischke style min eig
    coefficients: dict[str, float]  # first-stage coefs on Z (and X)


@dataclass(frozen=True)
class IdentificationResult:
    order_condition: bool
    order_detail: str
    rank_condition: bool
    rank_value: int
    rank_required: int
    instrument_correlation_matrix: list[list[float]]
    instrument_correlation_min: float
    collinear_instrument_pairs: list[list[str]]
    cragg_donald_statistic: float
    status: Literal["identified", "weak", "unidentified"]
    reasons: list[str]


@dataclass(frozen=True)
class OveridResult:
    """Sargan (homoskedastic) / Wooldridge-robust score style test."""

    test_name: str
    testable: bool
    statistic: float | None
    p_value: float | None
    degrees_of_freedom: int | None
    verdict: str  # "not_reject" | "reject" | "untestable"
    detail: str


@dataclass(frozen=True)
class EndogeneityResult:
    """Durbin-Wu-Hausman variant: Hausman contrast OLS vs IV."""

    statistic: float
    p_value: float
    degrees_of_freedom: int
    verdict: str  # "endogenous" | "not_endogenous" | "inconclusive"
    ols_contrast: list[float]
    ivs_contrast: list[float]


@dataclass(frozen=True)
class EstimationOutcome:
    request_id: str
    status: Literal["ok", "weak", "inconclusive"]
    coefficients: list[CoefficientResult]
    first_stage: list[FirstStageResult]
    identification: IdentificationResult
    overidentification: OveridResult
    endogeneity: EndogeneityResult
    assumptions: dict[str, object]
    covariance_kind: str
    nobs: int
    n_endog: int
    n_exog: int
    n_instruments: int
    degrees_of_freedom_resid: int
    sigma2: float
    warnings: tuple[str, ...] = ()

    def coefficient_map(self) -> dict[str, CoefficientResult]:
        return {c.name: c for c in self.coefficients}
