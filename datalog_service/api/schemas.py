"""Pydantic request/response schemas."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class SubmitRequest(BaseModel):
    program: str = Field(..., description="Datalog facts and rules source text")


class QueryRequest(BaseModel):
    query: str = Field(..., description="Goal atom ending with '?'")
    include_proofs: bool = True
    max_answers: Optional[int] = Field(
        default=None, ge=1, le=10000, description="Cap answers returned"
    )


class RuleInfo(BaseModel):
    rule_id: str
    rule_hash: str
    stratum: int
    text: str


class PredicateInfoModel(BaseModel):
    name: str
    arity: int
    defined_by_facts: bool
    defined_by_rules: bool
    stratum: int


class SubmitResponse(BaseModel):
    request_id: str
    status: str
    program_id: str
    materialization_id: str
    rule_version: str
    fact_set_version: str
    reused: bool
    rule_count: int
    fact_count: int
    strata: List[List[str]]
    predicates: Dict[str, PredicateInfoModel]
    evaluation: Dict[str, Any]
    failures: List[Dict[str, Any]] = []


class AnswerModel(BaseModel):
    bindings: Dict[str, Any]
    proof: Optional[Dict[str, Any]] = None


class QueryResponse(BaseModel):
    request_id: str
    status: str
    program_id: str
    materialization_id: str
    goal: str
    variables: List[str]
    answer_count: int
    truncated: bool
    answers: List[AnswerModel]
    rule_version: str
    fact_set_version: str
    failures: List[Dict[str, Any]] = []
    uncertainty: List[Dict[str, Any]] = []


class ErrorResponse(BaseModel):
    request_id: str
    status: str = "error"
    error_code: str
    message: str
    details: Dict[str, Any] = {}
