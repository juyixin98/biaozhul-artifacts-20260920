"""FastAPI HTTP service around the bounded-order settlement contracts.

Only intended for the local Anvil test chain. Run:
    uvicorn app.main:app --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

import time
from dataclasses import asdict
from functools import lru_cache
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from web3 import Web3

from .chain import ChainError, LocalChain
from .config import Settings, load_deployment
from .signing import Order, order_hash, sign_order

app = FastAPI(
    title="Bounded Order Settlement API",
    version="1.0.0",
    description="Local-only signed-order settlement demo (Anvil + test keys).",
)


@lru_cache(maxsize=1)
def _state() -> tuple[Settings, LocalChain, dict]:
    settings = Settings.from_env()
    deployment = load_deployment(settings.deployment_file)
    chain = LocalChain(settings.rpc_url)
    if chain.chain_id != deployment["chain_id"]:
        raise RuntimeError(
            f"node chainId {chain.chain_id} != deployment chainId {deployment['chain_id']}"
        )
    return settings, chain, deployment


# --------------------------------------------------------------------- //
# Models                                                                //
# --------------------------------------------------------------------- //


class CreateOrderRequest(BaseModel):
    # Defaults use the pre-funded demo roles; all are overridable.
    makerAmount: int = Field(..., gt=0, description="gross maker token on offer")
    takerAmount: int = Field(..., gt=0, description="taker token owed for full fill")
    nonce: int = Field(..., ge=0)
    deadline: int | None = Field(None, description="unix seconds; default now + 3600")
    taker: str = Field("0x0000000000000000000000000000000000000000",
                       description="allowed taker; zero = anyone")
    feeRecipient: str = Field("0x0000000000000000000000000000000000000000")
    maxFeeAmount: int = Field(0, ge=0)
    makerRole: Literal["maker"] = "maker"  # demo uses the configured maker key


class OrderResponse(BaseModel):
    order: dict
    orderHash: str
    signature: str | None = None


class FillRequest(BaseModel):
    order: dict
    spent: int = Field(..., gt=0, description="gross maker token this fill consumes")
    fee: int = Field(0, ge=0)
    takerRole: Literal["taker"] = "taker"


class FillResponse(BaseModel):
    takerDue: int
    txHash: str
    blockNumber: int
    gasUsed: int
    fillStatus: dict


class CancelRequest(BaseModel):
    nonce: int
    makerRole: Literal["maker"] = "maker"


# --------------------------------------------------------------------- //
# Helpers                                                               //
# --------------------------------------------------------------------- //


def _parse_order(raw: dict) -> Order:
    required = {
        "maker", "taker", "makerToken", "takerToken", "makerAmount", "takerAmount",
        "nonce", "deadline", "feeRecipient", "maxFeeAmount",
    }
    missing = required - raw.keys()
    if missing:
        raise HTTPException(422, f"order missing fields: {sorted(missing)}")
    try:
        return Order(
            maker=Web3.to_checksum_address(raw["maker"]),
            taker=Web3.to_checksum_address(raw["taker"]),
            makerToken=Web3.to_checksum_address(raw["makerToken"]),
            takerToken=Web3.to_checksum_address(raw["takerToken"]),
            makerAmount=int(raw["makerAmount"]),
            takerAmount=int(raw["takerAmount"]),
            nonce=int(raw["nonce"]),
            deadline=int(raw["deadline"]),
            feeRecipient=Web3.to_checksum_address(raw["feeRecipient"]),
            maxFeeAmount=int(raw["maxFeeAmount"]),
        )
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, f"invalid order: {exc}") from exc


# --------------------------------------------------------------------- //
# Routes                                                                //
# --------------------------------------------------------------------- //


@app.get("/health")
def health() -> dict:
    settings, chain, deployment = _state()
    return {
        "status": "ok",
        "chainId": chain.chain_id,
        "blockNumber": chain.w3.eth.block_number,
        "settlement": deployment["settlement"],
    }


@app.get("/config")
def config() -> dict:
    settings, chain, deployment = _state()
    return {
        "chainId": chain.chain_id,
        "rpcUrl": settings.rpc_url,
        "settlement": deployment["settlement"],
        "tokenA": deployment["tokenA"],
        "tokenB": deployment["tokenB"],
        "accounts": deployment["accounts"],
    }


@app.post("/orders", response_model=OrderResponse)
def create_order(req: CreateOrderRequest) -> OrderResponse:
    settings, chain, deployment = _state()
    maker_key = settings.role_keys[req.makerRole]
    maker = chain.address_of(maker_key)
    deadline = req.deadline if req.deadline is not None else int(time.time()) + 3600

    order = Order(
        maker=maker,
        taker=Web3.to_checksum_address(req.taker),
        makerToken=Web3.to_checksum_address(deployment["tokenA"]),
        takerToken=Web3.to_checksum_address(deployment["tokenB"]),
        makerAmount=req.makerAmount,
        takerAmount=req.takerAmount,
        nonce=req.nonce,
        deadline=deadline,
        feeRecipient=Web3.to_checksum_address(req.feeRecipient),
        maxFeeAmount=req.maxFeeAmount,
    )
    sig = sign_order(maker_key, chain.chain_id, deployment["settlement"], order)
    h = order_hash(chain.chain_id, deployment["settlement"], order)
    return OrderResponse(order=asdict(order), orderHash=h, signature=sig)


@app.post("/orders/fill", response_model=FillResponse)
def fill_order(req: FillRequest) -> FillResponse:
    settings, chain, deployment = _state()
    order = _parse_order(req.order)
    settlement_addr = deployment["settlement"]

    if order.makerToken != deployment["tokenA"] or order.takerToken != deployment["tokenB"]:
        raise HTTPException(422, "order tokens must be the deployed tokenA/tokenB demo pair")

    sig = req.order.get("signature")
    if not sig:
        raise HTTPException(422, "order must include the maker 'signature' field")
    sig = str(sig)

    taker_key = settings.role_keys[req.takerRole]
    try:
        tx, taker_due = chain.fill_order(
            settlement_addr, taker_key, order.to_tuple(), req.spent, req.fee, sig
        )
        fill_h = order_hash(chain.chain_id, settlement_addr, order)
        status = chain.fill_status(settlement_addr, fill_h)
    except ChainError as exc:
        raise HTTPException(400, f"settlement reverted: {exc}") from exc
    return FillResponse(
        takerDue=taker_due,
        txHash=tx.tx_hash,
        blockNumber=tx.block_number,
        gasUsed=tx.gas_used,
        fillStatus=status,
    )


@app.post("/orders/cancel")
def cancel_nonce(req: CancelRequest) -> dict:
    settings, chain, deployment = _state()
    maker_key = settings.role_keys[req.makerRole]
    try:
        tx = chain.cancel_nonce(deployment["settlement"], maker_key, req.nonce)
    except ChainError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "nonce": req.nonce,
        "cancelled": True,
        "txHash": tx.tx_hash,
        "blockNumber": tx.block_number,
    }


@app.get("/orders/{order_hash}")
def get_order_status(order_hash: str) -> dict:
    settings, chain, deployment = _state()
    if len(order_hash) != 66 or not order_hash.startswith("0x"):
        raise HTTPException(422, "orderHash must be 0x-prefixed 32 bytes")
    status = chain.fill_status(deployment["settlement"], order_hash)
    return {"orderHash": order_hash, **status}


@app.get("/tokens/{token}/balance/{holder}")
def token_balance(token: str, holder: str) -> dict:
    settings, chain, deployment = _state()
    try:
        amount = chain.token_balance(token, holder)
    except Exception as exc:  # malformed token address etc.
        raise HTTPException(400, str(exc)) from exc
    return {"token": Web3.to_checksum_address(token),
            "holder": Web3.to_checksum_address(holder), "balance": amount}
