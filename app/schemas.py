"""FastAPI request/response schemas (web-layer DTOs only)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from .models import Rule, RuleMetrics, RuleStatus


class IngestRequest(BaseModel):
    name: str = Field(..., min_length=1, description="unique dataset name")
    transactions: List[List[str]] = Field(..., description="list of item lists")
    min_support: float = Field(..., gt=0.0, le=1.0)
    max_length: Optional[int] = Field(default=None, ge=2)
    overwrite: bool = False


class RuleRequest(BaseModel):
    min_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    min_lift: Optional[float] = Field(default=None, ge=0.0)
    min_leverage: Optional[float] = Field(default=None, ge=-1.0, le=1.0)
    max_rules: Optional[int] = Field(default=None, ge=1)
    antecedent: Optional[List[str]] = None
    consequent: Optional[List[str]] = None


class MetricsOut(BaseModel):
    support_count: int
    support: float
    confidence: Optional[float]
    lift: Optional[float]
    leverage: float
    antecedent_count: int
    consequent_count: int
    antecedent_support: float
    consequent_support: float
    confidence_denominator: int
    lift_denominator: Optional[float]
    defined: bool
    undefined_reason: Optional[str]


class RuleOut(BaseModel):
    antecedent: List[str]
    consequent: List[str]
    metrics: MetricsOut
    status: RuleStatus
    reasons: List[str]
    warnings: List[str]


class RuleResponse(BaseModel):
    dataset_id: int
    dataset_name: str
    request_id: str
    n_rules: int
    rules: List[RuleOut]
    diagnostics: List[Dict[str, Any]]


class IngestResponse(BaseModel):
    dataset_id: int
    dataset_name: str
    n_transactions: int
    n_itemsets: int
    diagnostics: List[Dict[str, Any]]


class ValidationIssueOut(BaseModel):
    field: str
    reason: str
    detail: str


class ErrorResponse(BaseModel):
    request_id: str
    error: str
    issues: List[ValidationIssueOut] = []


def metrics_to_out(m: RuleMetrics) -> MetricsOut:
    return MetricsOut(
        support_count=m.support_count,
        support=m.support,
        confidence=m.confidence,
        lift=m.lift,
        leverage=m.leverage,
        antecedent_count=m.antecedent_count,
        consequent_count=m.consequent_count,
        antecedent_support=m.antecedent_support,
        consequent_support=m.consequent_support,
        confidence_denominator=m.confidence_denominator,
        lift_denominator=m.lift_denominator,
        defined=m.defined,
        undefined_reason=m.undefined_reason,
    )


def rule_to_out(rule: Rule) -> RuleOut:
    return RuleOut(
        antecedent=list(rule.antecedent),
        consequent=list(rule.consequent),
        metrics=metrics_to_out(rule.metrics),
        status=rule.status,
        reasons=list(rule.reasons),
        warnings=list(rule.warnings),
    )
