"""Pydantic API schemas -- explicit request/response contracts."""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field, field_validator

from ..contracts import EffectScale, TestDirection


class _BasePlanRequest(BaseModel):
    alpha: float = Field(..., gt=0.0, lt=1.0, description="significance level")
    power: float = Field(..., gt=0.0, lt=1.0, description="target power, must exceed alpha")
    direction: TestDirection
    allocation_ratio: float = Field(1.0, gt=0.0, description="n1 / n0")
    run_label: Optional[str] = Field(None, description="optional label echoed into the run log")

    @field_validator("power")
    @classmethod
    def _power_above_alpha(cls, v: float, info) -> float:
        alpha = info.data.get("alpha")
        if alpha is not None and v <= alpha:
            raise ValueError("target power must be strictly greater than alpha")
        return v


class NormalPlanRequest(_BasePlanRequest):
    one_sample: bool = False
    standardized_effect: Optional[float] = Field(
        None, description="Cohen's d; may be 0 only in a diagnostic zero-effect probe")
    delta: Optional[float] = Field(None, description="raw mean difference mu1 - mu0")
    sd0: float = Field(1.0, gt=0.0)
    sd1: Optional[float] = Field(None, gt=0.0)
    use_t_distribution: bool = False


class BinomialPlanRequest(_BasePlanRequest):
    p0: float = Field(..., gt=0.0, lt=1.0)
    p1: float = Field(..., gt=0.0, lt=1.0)
    one_sample: bool = False
    scale: EffectScale = EffectScale.DIFFERENCE
    force_exact: bool = Field(
        False, description="skip the normal approximation and solve the exact binomial test")


class SimulationRequest(BaseModel):
    family: str = Field(..., pattern="^(normal|binomial)$")
    n_per_group0: int = Field(..., ge=1)
    n_per_group1: Optional[int] = Field(None, ge=1)
    alpha: float = Field(..., gt=0.0, lt=1.0)
    direction: TestDirection
    # normal
    standardized_effect: Optional[float] = None
    sd0: float = 1.0
    sd1: Optional[float] = None
    use_t_distribution: bool = False
    # binomial
    p0: Optional[float] = Field(None, gt=0.0, lt=1.0)
    p1: Optional[float] = Field(None, gt=0.0, lt=1.0)
    exact_one_sample: bool = True
    # simulation control
    replications: int = Field(20_000, ge=100, le=2_000_000)
    seed: Optional[int] = None
    target_power: Optional[float] = Field(
        None, description="if given, agreement with the CI is assessed against it")


class InterimPlanRequest(BaseModel):
    information_times: List[float] = Field(..., min_length=1)
    alpha: float = Field(..., gt=0.0, lt=1.0)
    direction: TestDirection
    family: str = Field("obrien_fleming", pattern="^(obrien_fleming|pocock|fixed)$")
    drift_at_full_information: float = Field(
        ..., description="non-centrality the fixed-N design would have at total N")
    max_looks: int = Field(20, ge=1, le=20)


class PlanResponse(BaseModel):
    run_id: str
    success: bool
    family: str
    n_per_group0: Optional[int]
    n_per_group1: Optional[int]
    n_total: Optional[int]
    achieved_power: Optional[float]
    method: Optional[str]
    failure_category: str
    failure_detail: Optional[str]
    power_at_n_minus_one: Optional[float]
    diagnostics: dict


class SimulationResponse(BaseModel):
    run_id: str
    replications: int
    estimated_power: float
    standard_error: float
    ci95: List[float]
    target_power: Optional[float]
    agrees_with_target: Optional[bool]
    seed: int


class InterimResponse(BaseModel):
    run_id: str
    family: str
    information_times: List[float]
    z_boundaries: List[float]
    cumulative_alpha_spent: List[float]
    per_look_nominal_alpha: List[float]
    alternative_power: float
    fixed_sample_power: float
    power_loss_vs_fixed: float


class ErrorEnvelope(BaseModel):
    run_id: Optional[str]
    error: str
    failure_category: str
    detail: str
