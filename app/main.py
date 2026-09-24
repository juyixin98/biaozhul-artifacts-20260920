"""FastAPI 服务入口（纯后端，无前端页面）。

启动：
    uvicorn app.main:app --host 0.0.0.0 --port 8000

所有 POST 写接口要求真实 HMAC-SHA256 签名（X-Timestamp / X-Signature 头），
客户端助手见 app.crypto.sign_request 与 examples/send_signed.py。
"""
from __future__ import annotations

import time
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from . import __version__
from .config import settings
from .crypto import verify_request
from .fusion import FusionEngine
from .schemas import Health, Measurement, MeasurementBatch

app = FastAPI(
    title="异步 EKF 融合服务",
    version=__version__,
    description="二维位置/速度 EKF，按测量时间融合，支持 2s 迟到重放、稳定协方差与门限拒绝。",
)

engine = FusionEngine()


# -------------------------------------------------------------- auth
async def require_signature(
    request: Request,
    x_timestamp: str | None = Header(default=None),
    x_signature: str | None = Header(default=None),
) -> dict[str, Any]:
    raw = await request.body()
    ok, reason = verify_request(
        request.method,
        request.url.path,
        x_timestamp,
        x_signature,
        raw,
        now=time.time(),
    )
    if not ok:
        # 依赖中必须抛异常才能中断请求
        raise HTTPException(
            status_code=401,
            detail={"error": "unauthorized", "reason": reason, "authenticated": False},
            headers={"WWW-Authenticate": "HMAC-SHA256"},
        )
    return {"raw": raw}


# -------------------------------------------------------------- helpers
def _run_measurement(m: Measurement) -> dict[str, Any]:
    return engine.ingest(
        mid=m.id,
        mtype=m.type,
        t=m.time,
        measurement=m.measurement,
        R=m.R,
        seq=m.seq,
        gate_nis=m.gate_nis,
    )


# -------------------------------------------------------------- endpoints
@app.get("/health", response_model=Health)
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "initialized": engine.x is not None,
        "buffered": len(engine._buffer),
        "checkpoints": len(engine._checkpoints),
        "latest_time": engine.latest_time,
    }


@app.post("/fuse", dependencies=[Depends(require_signature)])
def fuse(m: Measurement) -> JSONResponse:
    """融合单条测量。离群/超窗拒绝返回 200 且 accepted=false（证据见 evidence/step）；
    协议非法（协方差等）返回 422。"""
    result = _run_measurement(m)
    if result["reason"] == "BAD_COVARIANCE":
        return JSONResponse(status_code=422, content=result)
    return JSONResponse(status_code=200, content=result)


@app.post("/fuse/batch", dependencies=[Depends(require_signature)])
def fuse_batch(batch: MeasurementBatch) -> dict[str, Any]:
    """批量融合：逐条按到达顺序喂入引擎，引擎内部仍按测量时间重放排序。
    结果数组与输入一一对应。"""
    results = [_run_measurement(m) for m in batch.measurements]
    accepted = sum(1 for r in results if r["accepted"])
    return {
        "received": len(results),
        "accepted": accepted,
        "rejected": len(results) - accepted,
        "results": results,
        "state": engine.state_view(),
    }


@app.get("/state")
def get_state() -> dict[str, Any]:
    return engine.state_view()


@app.get("/trace")
def get_trace(limit: int = 500) -> dict[str, Any]:
    limit = max(1, min(limit, 5000))
    entries = engine.trace_view(limit=limit)
    return {"count": len(entries), "steps": entries}


@app.get("/rejections")
def get_rejections(limit: int = 100) -> dict[str, Any]:
    limit = max(1, min(limit, 1000))
    entries = engine.rejections_view(limit=limit)
    return {"count": len(entries), "rejections": entries}


@app.post("/reset", dependencies=[Depends(require_signature)])
def reset() -> dict[str, Any]:
    engine.reset()
    return {"status": "reset", "state": engine.state_view()}
