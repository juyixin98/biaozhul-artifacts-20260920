"""FastAPI 应用: 离线借贷清算回放服务 (纯后端, 无前端)。

启动时自动建表、加载/生成服务端 Ed25519 签名密钥。
受信任事件签名公钥通过 ``LIQREPLAY_OPERATOR_KEYS`` (逗号分隔 hex) 配置。
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from .config import settings
from .crypto import load_or_create_server_key, public_hex, public_key_from_hex
from .repository import MemoryRepo, PostgresRepo
from .schemas import (
    EventIn,
    Health,
    IngestResult,
    ReportSummary,
    VerifyResult,
)
from .service import ReplayService

# 允许在无 Postgres 的环境 (如部分 CI) 下用内存仓储启动, 便于冒烟。
_repo = MemoryRepo() if settings.database_url == "memory" else PostgresRepo(
    settings.database_url
)
_service: ReplayService | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _service
    _repo.init_schema()
    if _service is None:  # 测试可预先注入内存服务
        server_key = load_or_create_server_key(settings.keys_dir)
        trusted = {}
        for hexkey in settings.operator_key_list():
            trusted[hexkey.lower()] = public_key_from_hex(hexkey)
        _service = ReplayService(_repo, trusted, server_key)
    app.state.server_public_hex = public_hex(_service.server_private_key)
    yield


app = FastAPI(
    title="借贷清算回放器 (Liquidation Replayer)",
    version="1.0.0",
    description="离线抵押借贷回放: 本地签名事件 + 分段利率 + 确定性版本化重算",
    lifespan=lifespan,
)


def service() -> ReplayService:
    assert _service is not None, "service not initialized"
    return _service


@app.post("/api/v1/events", response_model=IngestResult)
def ingest_events(events: list[EventIn]) -> dict:
    """提交一批签名事件。去重 -> 验签 -> 全量重算 -> (按需)生成新版本。"""
    if not events:
        raise HTTPException(status_code=400, detail="empty batch")
    raw = [e.model_dump() for e in events]
    return service().ingest(raw)


@app.get("/api/v1/reports/latest")
def latest_report():
    v = service().repo.get_latest_version()
    if v is None:
        raise HTTPException(status_code=404, detail="no report yet")
    rec = service().repo.get_report(v)
    return rec


@app.get("/api/v1/reports/{version}")
def get_report(version: int):
    if version < 1:
        raise HTTPException(status_code=404, detail="bad version")
    rec = service().repo.get_report(version)
    if rec is None:
        raise HTTPException(status_code=404, detail="not found")
    return rec


@app.get("/api/v1/reports", response_model=list[ReportSummary])
def list_reports(limit: int = 50):
    return service().repo.list_reports(limit=limit)


@app.get("/api/v1/liquidations")
def liquidations():
    v = service().repo.get_latest_version()
    if v is None:
        return {"version": None, "liquidations": []}
    rec = service().repo.get_report(v)
    return {"version": v, "liquidations": rec["body"]["liquidations"]}


@app.get("/api/v1/replay")
def replay(as_of: int | None = None):
    """只读回放当前存储事件; 不生成版本。"""
    return service().replay_preview(as_of=as_of)


@app.get("/api/v1/reports/{version}/verify", response_model=VerifyResult)
def verify_report(version: int):
    rec = service().verify_report(version)
    if rec is None:
        raise HTTPException(status_code=404, detail="not found")
    return rec


@app.get("/health", response_model=Health)
def health() -> Health:
    db = "ok"
    latest = None
    try:
        service().repo.ping()
        latest = service().repo.get_latest_version()
    except Exception as exc:  # noqa: BLE001
        db = f"error: {exc.__class__.__name__}"
    return Health(status="ok" if db == "ok" else "degraded", db=db, latest_version=latest)


@app.get("/")
def root():
    return {
        "service": "liqreplay",
        "version": "1.0.0",
        "server_public_hex": app.state.server_public_hex if _service else None,
        "trusted_signers": settings.operator_key_list(),
        "docs": "/docs",
    }
