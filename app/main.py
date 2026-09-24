"""Lossless precision fee settlement — FastAPI service over a local Anvil chain.

Run:
    anvil --port 8545 &
    uvicorn app.main:app --port 8000

The service deploys (or attaches to) LosslessFeeSettlement and exposes the
contract over HTTP. All fee math happens on-chain in integer arithmetic with
remainder carry; this layer only signs transactions and formats results.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from web3 import Web3
from web3.exceptions import ContractLogicError

ROOT = Path(__file__).resolve().parent.parent
ARTIFACT = ROOT / "out" / "LosslessFeeSettlement.sol" / "LosslessFeeSettlement.json"

RPC_URL = os.environ.get("RPC_URL", "http://127.0.0.1:8545")
# Anvil's well-known first dev key; local test chains only, never mainnet.
PRIVATE_KEY = os.environ.get(
    "PRIVATE_KEY",
    "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80",
)
CONTRACT_ADDRESS = os.environ.get("CONTRACT_ADDRESS", "")

RATE_SCALE = 10**18

app = FastAPI(title="Lossless Fee Settlement", version="1.0.0")

w3 = Web3(Web3.HTTPProvider(RPC_URL))
account = w3.eth.account.from_key(PRIVATE_KEY)
contract = None  # set in lifespan startup


def _load_artifact() -> dict[str, Any]:
    if not ARTIFACT.exists():
        raise RuntimeError(
            f"contract artifact not found at {ARTIFACT}; run `forge build` first"
        )
        # unreachable, keeps type checkers happy
    return json.loads(ARTIFACT.read_text())


@app.on_event("startup")
def startup() -> None:
    global contract
    if not w3.is_connected():
        raise RuntimeError(f"cannot reach JSON-RPC at {RPC_URL}; is anvil running?")
    artifact = _load_artifact()
    if CONTRACT_ADDRESS:
        address = Web3.to_checksum_address(CONTRACT_ADDRESS)
    else:
        factory = w3.eth.contract(abi=artifact["abi"], bytecode=artifact["bytecode"]["object"])
        tx_hash = factory.constructor().transact({"from": account.address})
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
        if receipt["status"] != 1:
            raise RuntimeError("contract deployment failed")
        address = receipt["contractAddress"]
    contract = w3.eth.contract(address=address, abi=artifact["abi"])


def _transact(fn) -> dict[str, Any]:
    """Build, sign, send and wait for a contract call; surface reverts as 4xx."""
    try:
        tx = fn.build_transaction(
            {
                "from": account.address,
                "nonce": w3.eth.get_transaction_count(account.address),
                "gas": 3_000_000,
            }
        )
        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    except ContractLogicError as exc:
        raise HTTPException(status_code=422, detail=f"contract revert: {exc}") from exc
    if receipt["status"] != 1:
        # Re-simulate to fetch the revert reason.
        try:
            fn.call({"from": account.address})
        except ContractLogicError as exc:
            raise HTTPException(status_code=422, detail=f"contract revert: {exc}") from exc
        raise HTTPException(status_code=500, detail="transaction failed without reason")
    return receipt


def _account_view(address: str) -> dict[str, Any]:
    try:
        addr = Web3.to_checksum_address(address)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"bad address: {address}") from exc
    (
        principal,
        rate,
        last_block,
        accrued,
        remainder,
        pending_blocks,
        projected_fee,
        projected_remainder,
    ) = contract.functions.getAccount(addr).call()
    return {
        "address": addr,
        "principal": str(principal),
        "ratePerBlock": str(rate),
        "ratePerBlockFloat": rate / RATE_SCALE,
        "lastSettledBlock": last_block,
        "accruedFees": str(accrued),
        "remainder": str(remainder),
        "remainderFraction": remainder / RATE_SCALE,
        "pendingBlocks": pending_blocks,
        "projectedFees": str(projected_fee),
        "projectedRemainder": str(projected_remainder),
        "currentBlock": w3.eth.block_number,
    }


class OpenRequest(BaseModel):
    address: str = Field(..., description="account owner address")
    principal: int = Field(..., ge=0, description="fee-bearing base amount (integer base units)")
    ratePerBlock: int = Field(..., ge=0, le=RATE_SCALE, description="rate mantissa; 1e18 = 100%/block")


class RateRequest(BaseModel):
    address: str
    ratePerBlock: int = Field(..., ge=0, le=RATE_SCALE)


class PrincipalRequest(BaseModel):
    address: str
    principal: int = Field(..., ge=0)


class AddressRequest(BaseModel):
    address: str


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "connected": w3.is_connected(),
        "chainId": w3.eth.chain_id if w3.is_connected() else None,
        "block": w3.eth.block_number if w3.is_connected() else None,
        "contract": contract.address if contract else None,
        "operator": account.address,
    }


@app.post("/accounts", status_code=201)
def open_account(req: OpenRequest) -> dict[str, Any]:
    # NOTE: the contract keys accounts by msg.sender. This demo service signs
    # every call with the single operator key, so `address` must equal the
    # operator address; multi-user deployments would relay per-user keys.
    if Web3.to_checksum_address(req.address) != account.address:
        raise HTTPException(
            status_code=400,
            detail=f"demo service signs as {account.address}; pass that address",
        )
    receipt = _transact(contract.functions.open(req.principal, req.ratePerBlock))
    return {"txHash": receipt["transactionHash"].hex(), **_account_view(req.address)}


@app.get("/accounts/{address}")
def get_account(address: str) -> dict[str, Any]:
    return _account_view(address)


@app.post("/settle")
def settle(req: AddressRequest) -> dict[str, Any]:
    before = _account_view(req.address)
    receipt = _transact(contract.functions.settle())
    after = _account_view(req.address)
    return {
        "txHash": receipt["transactionHash"].hex(),
        "feeAdded": str(int(after["accruedFees"]) - int(before["accruedFees"])),
        "account": after,
    }


@app.post("/rate")
def set_rate(req: RateRequest) -> dict[str, Any]:
    receipt = _transact(contract.functions.setRate(req.ratePerBlock))
    return {"txHash": receipt["transactionHash"].hex(), **_account_view(req.address)}


@app.post("/principal")
def set_principal(req: PrincipalRequest) -> dict[str, Any]:
    receipt = _transact(contract.functions.setPrincipal(req.principal))
    return {"txHash": receipt["transactionHash"].hex(), **_account_view(req.address)}


@app.post("/claim")
def claim(req: AddressRequest) -> dict[str, Any]:
    receipt = _transact(contract.functions.claim())
    return {"txHash": receipt["transactionHash"].hex(), **_account_view(req.address)}


@app.post("/close")
def close(req: AddressRequest) -> dict[str, Any]:
    receipt = _transact(contract.functions.close())
    return {"txHash": receipt["transactionHash"].hex(), **_account_view(req.address)}


@app.get("/quote")
def quote(principal: int, ratePerBlock: int, blocks: int, carry: int = 0) -> dict[str, Any]:
    fee, carry_out = contract.functions.quote(principal, ratePerBlock, blocks, carry).call()
    return {"feeAdded": str(fee), "carryOut": str(carry_out)}


@app.post("/admin/mine")
def mine(blocks: int = 1) -> dict[str, Any]:
    """Advance the local Anvil chain (test helper, anvil-only RPC)."""
    if blocks < 1 or blocks > 100_000:
        raise HTTPException(status_code=400, detail="blocks must be in [1, 100000]")
    w3.provider.make_request("anvil_mine", [hex(blocks)])
    return {"block": w3.eth.block_number}
