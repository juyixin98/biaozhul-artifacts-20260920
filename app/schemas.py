"""Request/response Pydantic models.

Integer token amounts are transported as *strings*: JavaScript and many
clients lose precision above 2**53, while on-chain amounts are uint256.  The
``BigIntStr`` annotated type accepts a JSON number or string but always
validates/coerces to a Python ``int``.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, Field, field_validator

from .core import config


class _BigIntStr:
    """Accept int or numeric string; always produce int (never float).

    Serialized back as a string so uint256-scale amounts survive clients that
    lose precision above 2**53.
    """

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: Any):
        from pydantic_core import core_schema

        def validate(v: Any) -> int:
            if isinstance(v, bool):
                raise ValueError("boolean is not an integer amount")
            if isinstance(v, int):
                return v
            if isinstance(v, str):
                s = v.strip()
                if not s:
                    raise ValueError("empty amount")
                try:
                    return int(s, 10)
                except ValueError:
                    raise ValueError("amount must be an integer supplied as int or string")
            raise ValueError("amount must be an integer supplied as int or string")

        return core_schema.no_info_plain_validator_function(
            validate,
            serialization=core_schema.plain_serializer_function_ser_schema(
                str, return_schema=core_schema.str_schema(), when_used="json"
            ),
        )

    @classmethod
    def __get_pydantic_json_schema__(cls, field_schema: Any, handler: Any):
        field_schema.update(type="string", description="integer amount as a JSON string")


BigIntStr = Annotated[int, _BigIntStr()]


class QuoteRequest(BaseModel):
    reserve_in_units: BigIntStr = Field(..., description="Input-token reserve, integer token units")
    reserve_out_units: BigIntStr = Field(..., description="Output-token reserve, integer token units")
    amount_in_units: BigIntStr = Field(..., description="Input amount to sell, integer token units")
    decimals_in: int = Field(..., ge=config.DECIMALS_MIN, le=config.DECIMALS_MAX)
    decimals_out: int = Field(..., ge=config.DECIMALS_MIN, le=config.DECIMALS_MAX)
    amp: int = Field(
        ...,
        ge=config.AMP_MIN,
        le=config.AMP_MAX,
        description=f"Amplification A in [{config.AMP_MIN}, {config.AMP_MAX}]",
    )
    fee_bps: int = Field(
        default=config.FEE_BPS_DEFAULT,
        ge=config.FEE_BPS_MIN,
        le=config.FEE_BPS_MAX,
        description="Fee in basis points, charged on the input side",
    )
    max_iterations: int = Field(
        default=config.NEWTON_MAX_ITER_DEFAULT,
        ge=1,
        le=config.NEWTON_MAX_ITER_HARD_CAP,
        description="Newton iteration cap (hard ceiling enforced server-side)",
    )
    include_reference: bool = Field(
        default=True,
        description="Also run the independent bisection reference solver and cross-check roots",
    )

    @field_validator("reserve_in_units", "reserve_out_units", "amount_in_units")
    @classmethod
    def _non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("amounts must be non-negative")
        return v


class SolverReport(BaseModel):
    value_normalized: str
    iterations: int
    converged: bool
    residual_rel: str
    bisection_fallbacks: int
    max_iterations: int


class PostTradeReport(BaseModel):
    d_before_normalized: str
    d_reference_normalized: str
    d_after_normalized: str
    #: integer-execution rounding deviation vs the exact fee-inclusive state
    d_drift_rel: str
    #: legitimate D growth from the retained fee (surplus to LPs)
    fee_surplus_rel: str
    invariant_ok: bool


class SolverSection(BaseModel):
    d_newton: SolverReport
    d_bisection: SolverReport | None = None
    y_newton: SolverReport | None = None
    y_bisection: SolverReport | None = None
    roots_agree: bool
    root_agreement_rel: str
    post_trade: PostTradeReport | None = None


class QuotePayload(BaseModel):
    gross_out_normalized: str
    amount_out_units: str
    amount_out_normalized: str
    fee_out_units: str
    fee_out_normalized: str
    spot_price_before: str
    effective_price: str
    price_impact_bps: str


class ErrorBlock(BaseModel):
    code: str
    message: str


class QuoteResponse(BaseModel):
    quote_id: str
    tradable: bool
    error: ErrorBlock | None = None
    invariant: str
    amp_range: str
    precision_digits: int
    output_rounding: str
    pool: dict
    quote: QuotePayload | None = None
    solver: SolverSection
