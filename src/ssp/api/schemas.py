"""Pydantic API schemas (web boundary only).

The statistical core speaks immutable dataclasses (:mod:`ssp.contracts`);
these models validate the HTTP payload and convert to/from the core.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Direction = Literal["two_sided", "greater", "less"]
NormalScale = Literal["absolute_difference", "standardized_d"]
BinomialScale = Literal["risk_difference", "relative_risk", "odds_ratio", "proportions"]
Method = Literal["auto", "asymptotic", "exact"]


class _CommonPlanRequest(BaseModel):
    alpha: float = Field(..., gt=0, lt=1, description="Significance level")
    target_power: float = Field(..., gt=0, lt=1, description="Target power, strictly above alpha")
    alternative: Direction
    allocation_ratio: float = Field(1.0, gt=0, description="n1/n0 for two-sample plans")
    interim_looks: int = Field(1, ge=1, description="Must be 1: interim peeking is not priced here")
    method_preference: Method = "auto"


class NormalPlanRequest(_CommonPlanRequest):
    endpoint: Literal["normal"] = "normal"
    effect: float = Field(..., description="Mean difference (if absolute) or Cohen's d")
    effect_scale: NormalScale
    two_sample: bool = False
    sigma: float | None = Field(None, gt=0)
    known_sigma: bool = False
    mc_trials: int | None = Field(None, ge=100, le=200_000)


class BinomialPlanRequest(_CommonPlanRequest):
    endpoint: Literal["binomial"] = "binomial"
    p0: float = Field(..., gt=0, lt=1, description="Control/null proportion (strictly interior)")
    effect: float = Field(..., description="Effect on the declared scale")
    effect_scale: BinomialScale
    two_sample: bool = False
    mc_trials: int | None = Field(None, ge=100, le=200_000)


class ErrorResponse(BaseModel):
    status: Literal["failed"] = "failed"
    error_category: str
    message: str
    run_id: str
    input_fingerprint: str
    details: dict
    versions: dict
