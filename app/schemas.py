"""Pydantic schemas for the root-isolation HTTP boundary.

Request and response models are kept separate from the kernel's exact types;
rational numbers cross the boundary as canonical strings (``"3/7"``,
``"-1/1000000"``) so no binary-float rounding is introduced in transit.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class IsolationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    coefficients: list[Any] = Field(
        ...,
        description=(
            "Ascending-power rational coefficients: coefficient at index i "
            "multiplies x**i. Integers, decimal strings and 'p/q' strings "
            "are accepted and parsed exactly."
        ),
        min_length=1,
    )
    target_width: str = Field(
        default="1/1000000",
        description="Maximum rational width of each isolating open interval.",
    )
    interval_lo: Any = Field(
        default=None,
        description="Optional rational left endpoint of the search interval.",
    )
    interval_hi: Any = Field(
        default=None,
        description="Optional rational right endpoint of the search interval.",
    )
    request_id: str | None = Field(
        default=None,
        max_length=64,
        pattern=r"^[A-Za-z0-9._-]*$",
        description="Client-supplied correlation id; a random one is assigned "
                    "when absent.",
    )
    include_evidence: bool = Field(
        default=True,
        description="Attach the independent mpmath/SciPy/NumPy evidence report.",
    )


class Proof(BaseModel):
    method: str
    variations_left: int
    variations_right: int
    root_count_in_cell: int
    chain_length: int
    open_interval: bool
    convention: str


class RootOut(BaseModel):
    lo: str
    hi: str
    exact: bool
    multiplicity: int
    proof: Proof


class MultiplicityOut(BaseModel):
    multiplicity: int
    factor_degree: int
    sturm_chain_length: int
    cauchy_integer_bound: int
    distinct_real_roots: int


class EnclosureOut(BaseModel):
    point: str
    lower: str
    upper: str
    contains_zero: bool
    sign: int


class CellEvidenceOut(BaseModel):
    index: int
    verdict: str
    reasons: list[str]
    width: str
    width_ok: bool
    endpoint_lo: EnclosureOut
    endpoint_hi: EnclosureOut
    brentq_root: str | None
    numeric_roots: list[str]


class CrossCheckOut(BaseModel):
    method: str
    distinct_real_roots: int
    locations: list[str]
    reliable: bool
    note: str


class EvidenceOut(BaseModel):
    accepted: bool
    inconclusive: bool
    contradicted: bool
    summary: str
    cross_check: CrossCheckOut
    cells: list[CellEvidenceOut]


class FailureOut(BaseModel):
    code: str
    message: str
    state: dict[str, Any]


class IsolationResponse(BaseModel):
    request_id: str
    status: str
    degree: int | None
    is_zero_polynomial: bool
    roots: list[RootOut]
    multiplicities: list[MultiplicityOut]
    evidence: EvidenceOut | None
    diagnostics: dict[str, Any]
    failure: FailureOut | None
