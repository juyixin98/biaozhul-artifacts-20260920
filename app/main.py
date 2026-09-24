"""FastAPI application exposing the on-chain checkpoint history.

Endpoints
----------
GET  /health                 node + contract connectivity
POST /values                 set the value at the current block
GET  /values/latest          latest checkpoint, if any
GET  /values/at/{block}      last value at or before a block (400 if future)
GET  /checkpoints            raw checkpoint list (bounded, oldest/newest)
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from web3 import Web3

from .config import Settings, get_settings
from .contract import (
    CheckpointContract,
    FutureBlockError,
    LookupResult,
    deploy,
    load_abi,
)

_BYTECODE_PATH = Path(__file__).resolve().parent.parent / "out" / "Checkpoints.sol" / "Checkpoints.json"


class State:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.w3 = Web3(Web3.HTTPProvider(settings.rpc_url))
        if not self.w3.is_connected():
            raise RuntimeError(f"cannot connect to Ethereum node at {settings.rpc_url}")
        if settings.contract_address:
            self.checkpoints = CheckpointContract(
                self.w3, settings.contract_address, settings.private_key
            )
        else:
            # Convenience for local development: auto-deploy to the ephemeral
            # Anvil chain so `uvicorn app.main:app` works out of the box.
            bytecode = json.loads(_BYTECODE_PATH.read_text())["bytecode"]["object"]
            self.checkpoints = deploy(self.w3, settings.private_key, bytecode)


_state: State | None = None


def get_state() -> State:
    if _state is None:
        raise RuntimeError("application state not initialised")
    return _state


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _state
    # Tests (or embedders) may pre-build a State on app.state; otherwise we
    # construct one from environment configuration.
    _state = getattr(app.state, "prebuilt_state", None) or State(get_settings())
    yield
    _state = None


app = FastAPI(
    title="On-chain Block Checkpoints",
    version="1.0.0",
    description="Save values per block and query the last value at or before "
    "a target block. Same-block updates merge; future blocks are rejected. "
    "Local Anvil only.",
    lifespan=lifespan,
)


# ---------- schemas ----------

class SetValueIn(BaseModel):
    value: int = Field(..., ge=0, le=2**256 - 1, description="uint256 value")


class SetValueOut(BaseModel):
    block_number: int
    value: int
    merged: bool
    transaction_hash: str


class CheckpointOut(BaseModel):
    exists: bool
    block_number: int | None = None
    value: int | None = None


class RawCheckpoint(BaseModel):
    index: int
    block_number: int
    value: int


class HealthOut(BaseModel):
    rpc_url: str
    chain_id: int
    current_block: int
    contract_address: str
    checkpoint_count: int


# ---------- endpoints ----------

@app.get("/health", response_model=HealthOut, tags=["meta"])
def health(state: State = Depends(get_state)) -> HealthOut:
    cp = state.checkpoints
    return HealthOut(
        rpc_url=state.settings.rpc_url,
        chain_id=int(state.w3.eth.chain_id),
        current_block=cp.current_block(),
        contract_address=cp.address,
        checkpoint_count=cp.length(),
    )


@app.post("/values", response_model=SetValueOut, status_code=201, tags=["values"])
def set_value(body: SetValueIn, state: State = Depends(get_state)) -> SetValueOut:
    cp = state.checkpoints
    length_before = cp.length()
    receipt = cp.set_value(body.value)
    block_number = int(receipt["blockNumber"])
    merged = cp.length() == length_before
    return SetValueOut(
        block_number=block_number,
        value=body.value,
        merged=merged,
        transaction_hash=receipt["transactionHash"].to_0x_hex(),
    )


@app.get("/values/latest", response_model=CheckpointOut, tags=["values"])
def values_latest(state: State = Depends(get_state)) -> CheckpointOut:
    result = state.checkpoints.latest()
    if not result.exists:
        return CheckpointOut(exists=False)
    return CheckpointOut(
        exists=True, block_number=result.block_number, value=result.value
    )


@app.get("/values/at/{block}", response_model=CheckpointOut, tags=["values"])
def values_at(block: int, state: State = Depends(get_state)) -> CheckpointOut:
    if block < 0:
        raise HTTPException(status_code=422, detail="block must be non-negative")
    cp = state.checkpoints
    # Fast pre-check so future-block requests fail cleanly even if the error
    # decoding below ever drifts from the contract ABI.
    current = cp.current_block()
    if block > current:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "future_block",
                "requested_block": block,
                "current_block": current,
            },
        )
    try:
        result: LookupResult = cp.get_at_block(block)
    except FutureBlockError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "future_block",
                "requested_block": exc.requested,
                "current_block": exc.current,
            },
        ) from exc
    if not result.exists:
        return CheckpointOut(exists=False)
    return CheckpointOut(
        exists=True, block_number=result.block_number, value=result.value
    )


@app.get("/checkpoints", response_model=list[RawCheckpoint], tags=["inspect"])
def list_checkpoints(
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    state: State = Depends(get_state),
) -> list[RawCheckpoint]:
    cp = state.checkpoints
    total = cp.length()
    out: list[RawCheckpoint] = []
    for index in range(offset, min(offset + limit, total)):
        block_number, value = cp.contract.functions.checkpointAt(index).call()
        out.append(
            RawCheckpoint(
                index=index, block_number=int(block_number), value=int(value)
            )
        )
    return out


# Re-exported for tooling that imports the ABI from the app package.
__all__ = ["app", "load_abi"]
