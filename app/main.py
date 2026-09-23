"""FastAPI application: offline concentrated-liquidity quote service.

Endpoints
---------
GET  /health
POST /pools                         create an immutable pool snapshot
GET  /pools                         list snapshots
GET  /pools/{pool_id}               fetch snapshot + digest + price map
POST /pools/{pool_id}/quote         quote an exact-input swap (no reserves change)
GET  /quotes/{quote_id}             fetch a stored quote with evidence
POST /quotes/{quote_id}/verify      recompute SHA-256 digests and verify

All numeric state comes from SQLite; all money math is integer fixed-point.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException

from . import db
from .config import BASE_DEN, BASE_NUM, ENGINE_VERSION, FEE_DENOMINATOR, FEE_NUMERATOR, SQRT_DECIMALS, TICK_MAX, TICK_MIN
from .crypto import digest_pool, verify_quote_digest
from .engine import quote_swap
from .models import Pool, Position, QuoteResult, Segment
from .reference import reference_sqrt_price, verify_against_engine
from .schemas import CreatePool, QuoteRequest
from .tick import (
    format_fixed,
    price_at_tick_decimal,
    sqrt_price_at_tick,
    sqrt_price_at_tick_decimal,
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> Any:
    db.init_db()
    yield


app = FastAPI(
    title="Concentrated Liquidity Quote Engine",
    version=ENGINE_VERSION,
    description="Offline integer-tick, fixed-point concentrated-liquidity quote service.",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_pool_model(req: CreatePool) -> Pool:
    return Pool(
        pool_id=req.pool_id,
        token0=req.token0,
        token1=req.token1,
        fee_numerator=req.fee_numerator,
        fee_denominator=req.fee_denominator,
        current_tick=req.current_tick,
        positions=tuple(
            Position(p.lower_tick, p.upper_tick, p.liquidity) for p in req.positions
        ),
    )


def _result_from_dict(data: dict[str, Any]) -> QuoteResult:
    segs = tuple(
        Segment(
            index=s["index"],
            lower_tick=s["lower_tick"],
            upper_tick=s["upper_tick"],
            liquidity=s["liquidity"],
            start_sqrt_price=s["start_sqrt_price"],
            end_sqrt_price=s["end_sqrt_price"],
            input_token=s["input_token"],
            gross_input=s["gross_input"],
            curve_input=s["curve_input"],
            fee_input=s["fee_input"],
            output_token=s["output_token"],
            output=s["output"],
            crossed=s["crossed"],
            note=s.get("note", ""),
        )
        for s in data["segments"]
    )
    return QuoteResult(
        direction=data["direction"],
        input_token=data["input_token"],
        output_token=data["output_token"],
        amount_in=data["amount_in"],
        amount_out=data["amount_out"],
        unspent_input=data["unspent_input"],
        fee_paid=data["fee_paid"],
        curve_input_total=data["curve_input_total"],
        current_tick_before=data["current_tick_before"],
        current_tick_after=data["current_tick_after"],
        sqrt_price_before=data["sqrt_price_before"],
        sqrt_price_after=data["sqrt_price_after"],
        stop_reason=data["stop_reason"],
        segments=segs,
        pool_digest=data["pool_digest"],
        quote_digest=data["quote_digest"],
        engine_version=data.get("engine_version", ENGINE_VERSION),
    )


def _pool_detail(pool: Pool) -> dict[str, Any]:
    sp = pool.sqrt_price
    tick = pool.current_tick
    return {
        "pool_id": pool.pool_id,
        "token0": pool.token0,
        "token1": pool.token1,
        "fee_numerator": pool.fee_numerator,
        "fee_denominator": pool.fee_denominator,
        "fee_bps": pool.fee_numerator * 10_000 // pool.fee_denominator,
        "current_tick": tick,
        "tick_range": [TICK_MIN, TICK_MAX],
        "price_map": {
            "tick_base_numerator": BASE_NUM,
            "tick_base_denominator": BASE_DEN,
            "price": str(price_at_tick_decimal(tick)),
            "sqrt_price_fixed_point": str(sp),
            "sqrt_price_decimals": SQRT_DECIMALS,
            "sqrt_price_decimal": format_fixed(sp),
            "sqrt_price_high_precision": str(sqrt_price_at_tick_decimal(tick)),
            "rounding": "floor (integer fixed-point sqrt price at tick)",
        },
        "positions": [
            {
                "lower_tick": p.lower_tick,
                "upper_tick": p.upper_tick,
                "liquidity": str(p.liquidity),
            }
            for p in sorted(pool.positions, key=lambda p: (p.lower_tick, p.upper_tick))
        ],
        "interval_table": [
            {
                "index": k,
                "lower_tick": pool.boundaries[k],
                "upper_tick": pool.boundaries[k + 1],
                "active_liquidity": str(pool.interval_liquidity[k]),
            }
            for k in range(len(pool.boundaries) - 1)
        ],
        "pool_digest": digest_pool(pool),
        "engine_version": ENGINE_VERSION,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "engine_version": ENGINE_VERSION,
        "tick_range": [TICK_MIN, TICK_MAX],
        "sqrt_decimals": SQRT_DECIMALS,
        "default_fee_bps": FEE_NUMERATOR * 10_000 // FEE_DENOMINATOR,
    }


@app.post("/pools", status_code=201)
def create_pool(req: CreatePool) -> dict[str, Any]:
    if req.token0 == req.token1:
        raise HTTPException(status_code=400, detail="token0 and token1 must differ")
    if not 0 < req.fee_numerator < req.fee_denominator:
        raise HTTPException(status_code=400, detail="require 0 < fee_numerator < fee_denominator")
    try:
        pool = _build_pool_model(req)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if db.get_pool(req.pool_id) is not None:
        raise HTTPException(status_code=409, detail=f"pool_id {req.pool_id!r} already exists")
    db.insert_pool(req.pool_id, pool)
    return _pool_detail(pool)


@app.get("/pools")
def list_pools() -> dict[str, Any]:
    return {"pools": db.list_pools()}


@app.get("/pools/{pool_id}")
def get_pool(pool_id: str) -> dict[str, Any]:
    pool = db.get_pool(pool_id)
    if pool is None:
        raise HTTPException(status_code=404, detail=f"pool {pool_id!r} not found")
    return _pool_detail(pool)


@app.post("/pools/{pool_id}/quote", status_code=201)
def quote(pool_id: str, req: QuoteRequest, verify: bool = True) -> dict[str, Any]:
    pool = db.get_pool(pool_id)
    if pool is None:
        raise HTTPException(status_code=404, detail=f"pool {pool_id!r} not found")
    amount_in = int(req.amount_in)
    try:
        result = quote_swap(pool, zero_for_one=req.zero_for_one, amount_in=amount_in)
    except (ValueError, OverflowError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    payload = result.to_dict()

    verification: dict[str, Any] | None = None
    if verify:
        # Real cross-check: Decimal/Fraction slow reference over every segment.
        discrepancies = verify_against_engine(pool, result)
        verification = {
            "reference": "fractions.Fraction + decimal.Decimal (120 digits)",
            "ok": not discrepancies,
            "discrepancies": discrepancies,
        }
        if discrepancies:
            # The engine must agree with the independent reference; never lie.
            raise HTTPException(
                status_code=500,
                detail={"message": "engine/reference disagreement", "discrepancies": discrepancies},
            )

    quote_id = uuid.uuid4().hex
    db.insert_quote(
        quote_id,
        pool_id,
        request={"zero_for_one": req.zero_for_one, "amount_in": str(amount_in)},
        result=payload,
    )

    return {
        "quote_id": quote_id,
        "pool_id": pool_id,
        "request": {"zero_for_one": req.zero_for_one, "amount_in": str(amount_in)},
        "result": payload,
        "verification": verification,
    }


@app.get("/quotes/{quote_id}")
def get_quote(quote_id: str) -> dict[str, Any]:
    record = db.get_quote(quote_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"quote {quote_id!r} not found")
    return record


@app.post("/quotes/{quote_id}/verify")
def verify_quote(quote_id: str) -> dict[str, Any]:
    record = db.get_quote(quote_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"quote {quote_id!r} not found")
    pool = db.get_pool(record["pool_id"])
    if pool is None:
        raise HTTPException(status_code=410, detail="bound pool snapshot no longer exists")

    result = _result_from_dict(record["result"])
    digest_ok = verify_quote_digest(result)
    pool_ok = digest_pool(pool) == result.pool_digest
    discrepancies = verify_against_engine(pool, result)

    # Tick-map spot check embedded in the response: Decimal independence proof.
    tick = result.current_tick_after
    int_map = sqrt_price_at_tick(tick)
    dec_map = reference_sqrt_price(tick)

    return {
        "quote_id": quote_id,
        "quote_digest_ok": digest_ok,
        "pool_snapshot_digest_ok": pool_ok,
        "engine_matches_reference": not discrepancies,
        "reference_discrepancies": discrepancies,
        "tick_map_check": {
            "tick": tick,
            "integer_fixed_point": str(int_map),
            "decimal_reference": str(dec_map),
            "match": int_map == dec_map,
        },
        "verified": digest_ok and pool_ok and not discrepancies and int_map == dec_map,
    }
