# -*- coding: utf-8 -*-
"""FastAPI HTTP 接口（纯后端，无页面）。

接口总览见 README。所有请求/响应为 JSON；二进制字段用十六进制字符串。
"""
from __future__ import annotations

from typing import Optional

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import config
from .errors import AppError
from .storage import Database
from .engine import Engine


def create_app(db: Optional[Database] = None) -> FastAPI:
    database = db or Database(config.db_path())
    engine = Engine(database)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        # 崩溃恢复：启动即做完整性验证，失败直接抛出（不静默启动）
        app.state.integrity = engine.verify_integrity()
        yield

    app = FastAPI(
        title="IBC 包超时状态模型（教学简化）",
        version="1.0.0",
        description="真实哈希/签名/Merkle 证明驱动的 IBC 式包生命周期后端",
        lifespan=lifespan,
    )

    @app.exception_handler(AppError)
    async def app_error_handler(_: Request, exc: AppError):
        return JSONResponse(status_code=exc.status, content=exc.to_dict())

    app.state.db = database
    app.state.engine = engine

    # ------------------------------------------------------------------ health
    @app.get("/health")
    def health():
        return {
            "ok": True,
            "service": "ibc-mini",
            "model": "simplified teaching model — not production IBC",
            "chains": len(engine.list_chains()),
        }

    @app.get("/integrity")
    def integrity():
        return engine.verify_integrity()

    # ------------------------------------------------------------------ chains
    @app.post("/chains")
    def create_chain(body: dict):
        return engine.create_chain(
            body["chain_id"],
            revision_number=int(body.get("revision_number", 1)),
            genesis_time_nanos=body.get("genesis_time_nanos"),
        )

    @app.get("/chains")
    def list_chains():
        return engine.list_chains()

    @app.get("/chains/{chain_id}")
    def chain_info(chain_id: str):
        return engine.chain_info(chain_id)

    @app.post("/chains/{chain_id}/commit")
    def commit_block(chain_id: str, body: Optional[dict] = None):
        ts = (body or {}).get("time_nanos")
        return engine.commit_block(chain_id, ts)

    @app.get("/chains/{chain_id}/checkpoint")
    def latest_checkpoint(chain_id: str):
        return engine.latest_checkpoint(chain_id)

    @app.get("/chains/{chain_id}/proof")
    def get_proof(chain_id: str, key: str):
        # key 为状态键原文的十六进制（可用 /chains/{cid}/state-path 查询）
        return engine.get_proof(chain_id, key)

    @app.get("/chains/{chain_id}/packets")
    def list_packets(chain_id: str, channel_id: Optional[str] = None):
        return engine.list_packets(chain_id, channel_id)

    @app.get("/chains/{chain_id}/channels/{channel_id}/packets/{seq}")
    def packet_info(chain_id: str, channel_id: str, seq: int):
        return engine.packet_info(chain_id, channel_id, seq)

    @app.get("/chains/{chain_id}/escrow")
    def escrow(chain_id: str, channel_id: Optional[str] = None):
        return engine.escrow_accounts(chain_id, channel_id)

    # ------------------------------------------------------------------ clients
    @app.post("/clients")
    def create_client(body: dict):
        return engine.create_client(
            body["local_chain_id"],
            body["remote_chain_id"],
            client_id=body.get("client_id"),
            trusting_period_nanos=int(
                body.get("trusting_period_nanos", 24 * 60 * 60 * 1_000_000_000)
            ),
        )

    @app.get("/clients/{client_id}")
    def client_info(client_id: str):
        return engine.client_info(client_id)

    # ------------------------------------------------------------------ connections
    @app.post("/connections")
    def create_connection(body: dict):
        return engine.create_connection(body["conn_id"], body["chain_a"], body["chain_b"])

    # ------------------------------------------------------------------ channels
    @app.post("/channels/open-init")
    def chan_open_init(body: dict):
        return engine.channel_open_init(
            body["chain_id"], body["channel_id"], body["conn_id"],
            body["ordering"], body["version"], body["remote_channel_id"],
        )

    @app.post("/channels/open-try")
    def chan_open_try(body: dict):
        return engine.channel_open_try(
            body["chain_id"], body["channel_id"], body["conn_id"],
            body["ordering"], body["version"], body["remote_channel_id"],
            body["counterparty_version"], body["proof_init"],
        )

    @app.post("/channels/open-ack")
    def chan_open_ack(body: dict):
        return engine.channel_open_ack(
            body["chain_id"], body["channel_id"], body["proof_try"]
        )

    @app.post("/channels/open-confirm")
    def chan_open_confirm(body: dict):
        return engine.channel_open_confirm(
            body["chain_id"], body["channel_id"], body["proof_ack"]
        )

    @app.get("/chains/{chain_id}/channels/{channel_id}")
    def channel_info(chain_id: str, channel_id: str):
        return engine.channel_info(chain_id, channel_id)

    @app.post("/channels/close")
    def channel_close(body: dict):
        return engine.channel_close(body["chain_id"], body["channel_id"])

    # ------------------------------------------------------------------ packets
    @app.post("/packets/send")
    def send_packet(body: dict):
        return engine.send_packet(
            body["chain_id"],
            body["channel_id"],
            body["data"],
            int(body["timeout_height"]),
            int(body["timeout_time_nanos"]),
            amount=int(body.get("amount", 0)),
            sender=body.get("sender", "alice"),
            receiver=body.get("receiver", "bob"),
        )

    @app.post("/packets/recv")
    def recv_packet(body: dict):
        return engine.recv_packet(
            body["packet"], body["proof_commitment"], body.get("ack")
        )

    @app.post("/packets/acknowledge")
    def acknowledge_packet(body: dict):
        return engine.acknowledge_packet(body["packet"], body["ack"], body["proofs"])

    @app.post("/packets/timeout")
    def timeout_packet(body: dict):
        return engine.timeout_packet(body["packet"], body["proofs"])

    return app


app = create_app()
