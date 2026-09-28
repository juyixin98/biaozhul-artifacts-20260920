"""API and domain models.

These Pydantic models are the single source of truth for request/response
shapes; the mining kernel itself works on plain frozensets and dataclasses
so it stays usable without FastAPI.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class TransactionIn(BaseModel):
    transaction_id: str = Field(min_length=1, max_length=128)
    items: list[str]


class CorpusIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    transactions: list[TransactionIn] = Field(min_length=1)


class CorpusOut(BaseModel):
    corpus_id: int
    name: str
    n_transactions: int
    n_distinct_items: int
    n_duplicate_items_removed: int
    request_id: str


class MineRequest(BaseModel):
    min_support: float = Field(gt=0.0, le=1.0)


class ItemsetOut(BaseModel):
    items: list[str]
    support_count: int
    support: float


class MineResult(BaseModel):
    corpus_id: int
    min_support: float
    n_itemsets: int
    itemsets: list[ItemsetOut]
    request_id: str


class RuleQuery(BaseModel):
    min_confidence: float = Field(gt=0.0, le=1.0)
    min_lift: float | None = Field(default=None, gt=0.0)


class RuleEvaluateRequest(BaseModel):
    antecedent: list[str]
    consequent: list[str]


class RuleOut(BaseModel):
    rule_id: str
    antecedent: list[str]
    consequent: list[str]
    support: float | None
    confidence: float | None
    lift: float | None
    leverage: float | None
    warnings: list[str]


class RuleSetOut(BaseModel):
    corpus_id: int
    n_rules: int
    rules: list[RuleOut]
    request_id: str


class ApiError(BaseModel):
    """Structured error body; ``category`` is the machine-readable cause."""

    category: str
    detail: str
    request_id: str
