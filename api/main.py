"""FastAPI service exposing the local LinearTokenVesting contracts over HTTP.

All transactions are signed locally with Anvil test keys; the HTTP layer only
talks to the local chain at http://127.0.0.1:8545.
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from web3.exceptions import ContractLogicError

from . import config
from .client import VestingClient

app = FastAPI(
    title="Linear Token Vesting API",
    version="1.0.0",
    description="Local Anvil-backed cliff/linear vesting escrow query & control API.",
)

_client: VestingClient | None = None


def get_client() -> VestingClient:
    global _client
    if _client is None:
        _client = VestingClient()
    return _client


def _decode_revert(exc: Exception) -> str:
    """Best-effort extraction of the contract's revert reason."""
    if isinstance(exc, ContractLogicError):
        return exc.message or str(exc)
    msg = str(exc)
    for marker in ("execution reverted: ", "revert "):
        if marker in msg:
            return msg.split(marker, 1)[1].splitlines()[0]
    return msg


# ---------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------


class CreateScheduleRequest(BaseModel):
    beneficiary: str = Field(..., description="0x address receiving vested tokens")
    amount: int = Field(..., gt=0, description="token amount in base units")
    start_timestamp: int = Field(..., ge=0)
    cliff_duration: int = Field(..., ge=0, description="seconds with no vesting")
    vesting_duration: int = Field(..., gt=0, description="seconds from start to fully vested")
    revocable: bool = True
    owner_private_key: str | None = Field(
        None, description="defaults to Anvil test account #0"
    )


class ReleaseRequest(BaseModel):
    caller_private_key: str | None = Field(
        None, description="beneficiary (or owner) key; defaults to Anvil account #1"
    )


class RevokeRequest(BaseModel):
    owner_private_key: str | None = None


class FastForwardRequest(BaseModel):
    seconds: int = Field(..., gt=0, description="advance chain clock and mine one block")


# ---------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------


@app.get("/health")
def health() -> dict[str, Any]:
    try:
        status = get_client().chain_status()
    except Exception as exc:  # connection problems -> 503
        raise HTTPException(status_code=503, detail=f"chain unavailable: {exc}")
    return {"status": "ok", "chain": status}


@app.get("/schedules/{schedule_id}")
def get_schedule(schedule_id: int) -> dict[str, Any]:
    if schedule_id < 0:
        raise HTTPException(status_code=400, detail="schedule id must be >= 0")
    client = get_client()
    try:
        view = client.get_schedule(schedule_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=_decode_revert(exc))
    return _schedule_dict(view)


@app.post("/schedules", status_code=201)
def create_schedule(req: CreateScheduleRequest) -> dict[str, Any]:
    client = get_client()
    if req.cliff_duration > req.vesting_duration:
        raise HTTPException(
            status_code=400, detail="cliff_duration must be <= vesting_duration"
        )
    key = req.owner_private_key or config.OWNER_PRIVATE_KEY
    try:
        result = client.create_schedule(
            owner_key=key,
            beneficiary=req.beneficiary,
            amount=req.amount,
            start_timestamp=req.start_timestamp,
            cliff_duration=req.cliff_duration,
            vesting_duration=req.vesting_duration,
            revocable=req.revocable,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=_decode_revert(exc))
    return result


@app.post("/schedules/{schedule_id}/release")
def release(schedule_id: int, req: ReleaseRequest | None = None) -> dict[str, Any]:
    client = get_client()
    key = (
        (req.caller_private_key if req else None)
        or config.BENEFICIARY_PRIVATE_KEY
    )
    try:
        return client.release(key, schedule_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=_decode_revert(exc))


@app.post("/schedules/{schedule_id}/revoke")
def revoke(schedule_id: int, req: RevokeRequest | None = None) -> dict[str, Any]:
    client = get_client()
    key = (req.owner_private_key if req else None) or config.OWNER_PRIVATE_KEY
    try:
        return client.revoke(key, schedule_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=_decode_revert(exc))


@app.post("/test/evm/fast-forward")
def fast_forward(req: FastForwardRequest) -> dict[str, Any]:
    """Anvil-only helper used by the demo / integration tests."""
    client = get_client()
    target = client.sleep_chain(req.seconds)
    return {"new_timestamp": target}


@app.get("/tokens/{address}")
def token_balance(address: str) -> dict[str, Any]:
    client = get_client()
    try:
        return {
            "address": address,
            "balance": client.token_balance(address),
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _schedule_dict(v) -> dict[str, Any]:
    return {
        "id": v.id,
        "beneficiary": v.beneficiary,
        "total_amount": v.total_amount,
        "released_amount": v.released_amount,
        "refunded_amount": v.refunded_amount,
        "start": v.start,
        "cliff": v.cliff,
        "end": v.end,
        "revocable": v.revocable,
        "revoked": v.revoked,
        "revoked_at": v.revoked_at,
        "vested_now": v.vested_now,
        "releasable_now": v.releasable_now,
        "conservation_remaining": (
            v.total_amount - v.released_amount - v.refunded_amount
        ),
    }
