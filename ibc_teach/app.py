"""FastAPI 应用：双链 IBC 教学状态机的 HTTP 接口（纯后端，无前端）。

启动即从 SQLite 恢复；每个 Store 方法在写事务里提交（WAL + BEGIN IMMEDIATE），
崩溃后重启由 :meth:`Store.integrity_check` 重放块链、状态根与签名。
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from . import __version__
from .schemas import (
    ChannelCreateRequest,
    FinalizeBlockRequest,
    RelayRequest,
    SendPacketRequest,
)
from .store import IBCError, Store

DB_PATH = os.environ.get("IBC_TEACH_DB", "ibc_teach.db")


def create_app(db_path: str = DB_PATH) -> FastAPI:
    store = Store(db_path)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        # 启动：重放块链、状态根与签名；脏库直接拒绝服务。
        report = store.integrity_check()
        if not report["ok"]:
            raise RuntimeError(
                "integrity check failed on startup: " + "; ".join(report["problems"])
            )
        yield

    app = FastAPI(
        title="IBC Packet Timeout State Model (teaching, simplified)",
        version=__version__,
        description=(
            "单机模拟两条链与中继者的 IBC 风格包生命周期后端（教学模型，"
            "非 ibc-go 实现）。信任根为 Ed25519 签名检查点 + 稀疏 Merkle 状态根。"
        ),
        lifespan=lifespan,
    )

    @app.exception_handler(IBCError)
    async def _ibc_error_handler(_: Request, exc: IBCError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": exc.code, "message": str(exc)},
        )

    app.state.store = store

    # ---------- 基础 ----------

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "service": "ibc-teach", "version": __version__}

    @app.get("/chains")
    def chains() -> list[dict]:
        return store.list_chains()

    @app.get("/chains/{chain_id}/channels")
    def channels(chain_id: str) -> list[dict]:
        return store.list_channels(chain_id)

    @app.get("/packets")
    def packets(chain_id: Optional[str] = None) -> list[dict]:
        return store.list_packets(chain_id)

    @app.get("/admin/integrity")
    def integrity() -> dict:
        return store.integrity_check()

    # ---------- 通道 ----------

    @app.post("/channels", status_code=201)
    def create_channel(req: ChannelCreateRequest) -> dict:
        return store.create_channel(
            ordering=req.ordering,
            version=req.version,
            port_a=req.port_a,
            port_b=req.port_b,
            channel_a=req.channel_a,
            channel_b=req.channel_b,
        )

    @app.post("/chains/{chain_id}/channels/{port_id}/{channel_id}/close")
    def close_channel(chain_id: str, port_id: str, channel_id: str) -> dict:
        return store.close_channel(chain_id, port_id, channel_id)

    # ---------- 出块 ----------

    @app.post("/chains/{chain_id}/blocks")
    def finalize_block(chain_id: str, req: Optional[FinalizeBlockRequest] = None) -> dict:
        return store.finalize_block(
            chain_id,
            time_ns=req.time_ns if req else None,
            height=req.height if req else None,
        )

    # ---------- 发送 ----------

    @app.post("/packets/send", status_code=201)
    def send_packet(req: SendPacketRequest) -> dict:
        return store.send_packet(
            src_chain=req.src_chain,
            src_port=req.src_port,
            src_channel=req.src_channel,
            timeout_height=req.timeout_height,
            timeout_time_ns=req.timeout_time_ns,
            data_hex=req.data_hex,
            amount=req.amount,
        )

    # ---------- 中继证明查询 ----------

    @app.get("/chains/{chain_id}/proof")
    def proof(
        chain_id: str,
        height: int = Query(..., ge=0),
        path: str = Query(..., description="状态键的十六进制编码，可用 /keys 辅助构造"),
    ) -> dict:
        return store.proof(chain_id, height, bytes.fromhex(path))

    @app.get("/keys")
    def keys(
        kind: str = Query(..., pattern="^(commitment|receipt|next_seq_recv)$"),
        port: str = ...,
        channel: str = ...,
        sequence: int = Query(default=0, ge=0),
    ) -> dict:
        """把语义键编码成 proof 接口需要的十六进制 path。"""
        from . import store as st

        if kind == "commitment":
            k = st.commitment_key(port, channel, sequence)
        elif kind == "receipt":
            k = st.receipt_key(port, channel, sequence)
        else:
            k = st.next_seq_recv_key(port, channel)
        return {"path": k.hex()}

    # ---------- 跨链消息 ----------

    @app.post("/packets/recv")
    def recv_packet(req: RelayRequest) -> dict:
        return store.recv_packet(
            req.packet.model_dump(),
            req.checkpoint.model_dump(),
            req.proof.model_dump(),
        )

    @app.post("/packets/ack")
    def acknowledge_packet(req: RelayRequest) -> dict:
        return store.acknowledge_packet(
            req.packet.model_dump(),
            req.checkpoint.model_dump(),
            req.proof.model_dump(),
        )

    @app.post("/packets/timeout")
    def timeout_packet(req: RelayRequest) -> dict:
        return store.timeout_packet(
            req.packet.model_dump(),
            req.checkpoint.model_dump(),
            req.proof.model_dump(),
        )

    return app


app = create_app()
