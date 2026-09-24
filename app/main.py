"""FastAPI 应用：链上检查点查值的 HTTP 接口。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from web3 import Web3
from web3.exceptions import Web3Exception

from app.chain import (
    CheckpointClient,
    FutureBlockError,
    RpcUnavailableError,
    load_contract,
)
from app.config import Settings


class SetValueRequest(BaseModel):
    value: int = Field(..., ge=0, le=2**224 - 1)


class SetValueResponse(BaseModel):
    value: int
    blockNumber: int
    txHash: str
    status: int


class ValueResponse(BaseModel):
    targetBlock: int
    currentBlock: int
    value: int
    found: bool


class LatestResponse(BaseModel):
    blockNumber: int
    value: int
    exists: bool


class HistoryEntry(BaseModel):
    blockNumber: int
    value: int


class HealthResponse(BaseModel):
    status: str
    chainId: int | None = None
    currentBlock: int | None = None
    contract: str | None = None


def create_app(settings: Settings | None = None,
               client: CheckpointClient | None = None) -> FastAPI:
    app = FastAPI(
        title="On-chain Checkpoint Lookup",
        version="1.0.0",
        description=(
            "按区块保存数值检查点。同区块更新合并；"
            "GET /checkpoints/{block} 返回不晚于目标块的最后值，"
            "拒绝未来块查询。"
        ),
    )
    state: dict[str, Any] = {}

    if client is not None:
        state["client"] = client

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if client is None:
            s = settings or Settings.from_env()
            w3 = Web3(Web3.HTTPProvider(s.rpc_url, request_kwargs={"timeout": 10}))
            if not w3.is_connected():
                raise RpcUnavailableError(
                    f"cannot reach Ethereum node at {s.rpc_url}"
                )
            contract = load_contract(w3, s.contract_address, s.abi_path)
            state["client"] = CheckpointClient(
                w3, contract, s.private_key, s.receipt_timeout
            )
        yield

    app.router.lifespan_context = lifespan

    def _client() -> CheckpointClient:
        return state["client"]  # 测试注入或 startup 初始化

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        c = _client()
        return HealthResponse(
            status="ok",
            chainId=c.w3.eth.chain_id,
            currentBlock=c.w3.eth.block_number,
            contract=c.contract.address,
        )

    @app.post("/checkpoints", response_model=SetValueResponse, status_code=201)
    def set_checkpoint(body: SetValueRequest) -> SetValueResponse:
        c = _client()
        receipt = c.set_value(body.value)
        block = c.w3.eth.block_number
        return SetValueResponse(
            value=body.value,
            blockNumber=block,
            txHash=receipt["transactionHash"].hex(),
            status=receipt["status"],
        )

    @app.get("/checkpoints/{target_block}", response_model=ValueResponse)
    def get_checkpoint(target_block: int) -> Any:
        if target_block < 0:
            return JSONResponse(
                status_code=422,
                content={"detail": "target_block must be non-negative"},
            )
        c = _client()
        try:
            value = c.value_at(target_block)
        except FutureBlockError as exc:
            return JSONResponse(
                status_code=400,
                content={
                    "detail": str(exc),
                    "error": "future_block",
                    "requested": exc.requested,
                    "current": exc.current,
                },
            )
        current = c.w3.eth.block_number
        return ValueResponse(
            targetBlock=target_block,
            currentBlock=current,
            value=value,
            found=value != 0,
        )

    @app.get("/latest", response_model=LatestResponse)
    def get_latest() -> LatestResponse:
        latest = _client().latest()
        return LatestResponse(
            blockNumber=latest["blockNumber"],
            value=latest["value"],
            exists=latest["blockNumber"] != 0,
        )

    @app.get("/history", response_model=list[HistoryEntry])
    def get_history() -> list[HistoryEntry]:
        return [HistoryEntry(**e) for e in _client().history()]

    @app.exception_handler(RpcUnavailableError)
    def _rpc_error(_request: Any, exc: RpcUnavailableError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.exception_handler(Web3Exception)
    def _web3_error(_request: Any, exc: Web3Exception) -> JSONResponse:
        return JSONResponse(status_code=502, content={"detail": str(exc)})

    return app


app = create_app()
