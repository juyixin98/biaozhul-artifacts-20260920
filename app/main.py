"""FastAPI service exposing the on-chain linear-vesting escrow over HTTP.

Endpoints
----------
GET  /health                       node + deployment connectivity
POST /schedules                    create a vesting schedule
GET  /schedules/{id}               full schedule record
GET  /schedules/{id}/vested        vested amount at a time (default: now)
GET  /schedules/{id}/releasable    vested but not yet withdrawn
POST /schedules/{id}/release       claim vested tokens (to beneficiary)
POST /schedules/{id}/revoke        owner only: freeze curve, refund remainder
GET  /token                        synthetic token metadata
POST /token/approve                (helper) approve the escrow to spend tokens
POST /token/mint                   (helper) mint synthetic tokens

All state lives on the local chain. The server is a thin, stateless adapter.
"""
from __future__ import annotations

from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from web3.exceptions import ContractLogicError

from .chain import (
    account_from_key,
    deployed_handles,
    get_web3,
    load_addresses,
    send_contract_tx,
    wait_receipt,
)

app = FastAPI(
    title="Linear Token Vesting API",
    version="1.0.0",
    description="Query and drive a cliff + linear, revocable token escrow on local Anvil.",
)


# --------------------------------------------------------------------------
# Request / response models
# --------------------------------------------------------------------------
class CreateScheduleBody(BaseModel):
    beneficiary: str = Field(..., description="0x address receiving vested tokens")
    total_amount: int = Field(..., gt=0, description="token amount in base units")
    start: int = Field(..., ge=0, description="vesting curve start (unix seconds)")
    cliff: int = Field(..., ge=0, description="no release before this timestamp")
    end: int = Field(..., ge=0, description="vesting completes at this timestamp")


class MintBody(BaseModel):
    to: str
    amount: int = Field(..., gt=0)


class ApproveBody(BaseModel):
    spender: Optional[str] = None
    amount: int = Field(..., gt=0)


class TxResponse(BaseModel):
    tx_hash: str
    block_number: int
    status: int


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _handles():
    try:
        return deployed_handles()
    except (ConnectionError, FileNotFoundError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _call_view(fn):
    """Run a read-only contract call, turning reverts into 400s."""
    try:
        return fn.call()
    except ContractLogicError as exc:
        raise HTTPException(status_code=400, detail=f"contract reverted: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - surface node errors cleanly
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def _send(fn):
    w3 = deployed_handles()[0]
    account = account_from_key(w3)
    try:
        tx_hash = send_contract_tx(w3, account, fn)
        receipt = wait_receipt(w3, tx_hash)
    except ContractLogicError as exc:
        raise HTTPException(status_code=400, detail=f"contract reverted: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return TxResponse(
        tx_hash=tx_hash,
        status=int(receipt.status),
        block_number=int(receipt.blockNumber),
    )


def _schedule_dict(s) -> dict:
    keys = (
        "beneficiary",
        "token",
        "totalAmount",
        "start",
        "cliff",
        "end",
        "released",
        "revokedAt",
    )
    d = dict(zip(keys, s))
    # normalise python ints / checksummed addresses for JSON
    d["beneficiary"] = d["beneficiary"]
    d["token"] = d["token"]
    for k in ("totalAmount", "start", "cliff", "end", "released", "revokedAt"):
        d[k] = int(d[k])
    d["revoked"] = d["revokedAt"] != 0
    return d


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
@app.get("/health")
def health():
    w3, _token, vesting = _handles()
    try:
        addresses = load_addresses()
        return {
            "connected": True,
            "chain_id": int(w3.eth.chain_id),
            "block_number": int(w3.eth.block_number),
            "token": addresses["token"],
            "vesting": addresses["vesting"],
            "vesting_next_id": int(vesting.functions.nextScheduleId().call()),
        }
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/schedules", status_code=201, response_model=TxResponse)
def create_schedule(body: CreateScheduleBody):
    if not (body.start <= body.cliff <= body.end and body.start < body.end):
        raise HTTPException(
            status_code=422,
            detail="require start <= cliff <= end and start < end",
        )
    _w3, _token, vesting = _handles()
    fn = vesting.functions.createSchedule(
        body.beneficiary,
        _token.address,
        body.total_amount,
        body.start,
        body.cliff,
        body.end,
    )
    return _send(fn)


@app.get("/schedules/{schedule_id}")
def get_schedule(schedule_id: int):
    _w3, _token, vesting = _handles()
    raw = _call_view(vesting.functions.scheduleOf(schedule_id))
    return _schedule_dict(raw)


@app.get("/schedules/{schedule_id}/vested")
def get_vested(schedule_id: int, timestamp: Optional[int] = None):
    w3, _token, vesting = _handles()
    now_ts = int(w3.eth.get_block("latest")["timestamp"])
    ts = timestamp if timestamp is not None else now_ts
    vested = _call_view(vesting.functions.vestedAmount(schedule_id, int(ts)))
    return {"schedule_id": schedule_id, "timestamp": int(ts), "vested": int(vested)}


@app.get("/schedules/{schedule_id}/releasable")
def get_releasable(schedule_id: int):
    w3, _token, vesting = _handles()
    amount = _call_view(vesting.functions.releasable(schedule_id))
    return {
        "schedule_id": schedule_id,
        "timestamp": int(w3.eth.get_block("latest")["timestamp"]),
        "releasable": int(amount),
    }


@app.post("/schedules/{schedule_id}/release", response_model=TxResponse)
def release_schedule(schedule_id: int):
    _w3, _token, vesting = _handles()
    return _send(vesting.functions.release(schedule_id))


@app.post("/schedules/{schedule_id}/revoke", response_model=TxResponse)
def revoke_schedule(schedule_id: int):
    _w3, _token, vesting = _handles()
    return _send(vesting.functions.revoke(schedule_id))


@app.get("/token")
def token_info():
    w3, token, _vesting = _handles()
    account = account_from_key(w3)
    return {
        "address": token.address,
        "name": _call_view(token.functions.name()),
        "symbol": _call_view(token.functions.symbol()),
        "decimals": int(_call_view(token.functions.decimals())),
        "total_supply": int(_call_view(token.functions.totalSupply())),
        "deployer": account.address,
        "deployer_balance": int(_call_view(token.functions.balanceOf(account.address))),
    }


@app.post("/token/mint", response_model=TxResponse)
def mint_tokens(body: MintBody):
    _w3, token, _vesting = _handles()
    return _send(token.functions.mint(body.to, body.amount))


@app.post("/token/approve", response_model=TxResponse)
def approve_tokens(body: ApproveBody):
    _w3, token, vesting = _handles()
    spender = body.spender or vesting.address
    return _send(token.functions.approve(spender, body.amount))
