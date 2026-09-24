"""FastAPI 入口：会话管理与逐帧关联接口。"""

from __future__ import annotations

import secrets

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from .schemas import (
    CreateSessionIn,
    FrameIn,
    FrameOut,
    HealthOut,
    SessionOut,
    TrackerConfigIn,
)
from .storage import SessionStore, process_frame
from .tracker import StaleFrameError, TrackerConfig

app = FastAPI(
    title="多目标轨迹关联 API",
    version="1.0.0",
    description=(
        "匀速 Kalman 预测 + Mahalanobis 门控 + 匈牙利分配的纯后端 2D MOT 服务。"
        "乱序帧拒绝、重复帧幂等，响应带 HMAC-SHA256 签名。"
    ),
)
store = SessionStore()


@app.get("/health", response_model=HealthOut, tags=["meta"])
def health() -> HealthOut:
    return HealthOut(status="ok", sessions=len(store))


@app.post("/sessions", response_model=SessionOut, status_code=201, tags=["sessions"])
def create_session(req: CreateSessionIn) -> SessionOut:
    sid = req.session_id or secrets.token_hex(8)
    cfg_in: TrackerConfigIn | None = req.config
    cfg = (
        TrackerConfig(**cfg_in.model_dump())
        if cfg_in is not None
        else TrackerConfig()
    )
    try:
        session = store.create(sid, config=cfg)
    except KeyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    t = session.tracker
    return SessionOut(
        session_id=sid,
        config=vars(t.config),
        last_frame_id=t._last_frame_id,
        last_timestamp=t._last_timestamp,
        alive_track_count=len(t.tracks),
        next_track_id=t._next_id,
        signing_key_hex=session.signing_key_hex,
    )


@app.get("/sessions/{session_id}", response_model=SessionOut, tags=["sessions"])
def get_session(session_id: str) -> SessionOut:
    try:
        session = store.get(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    with session._lock:
        t = session.tracker
        return SessionOut(
            session_id=session_id,
            config=vars(t.config),
            last_frame_id=t._last_frame_id,
            last_timestamp=t._last_timestamp,
            alive_track_count=sum(1 for tr in t.tracks.values() if tr.alive),
            next_track_id=t._next_id,
            signing_key_hex=None,
        )


@app.post(
    "/sessions/{session_id}/frames",
    response_model=FrameOut,
    tags=["frames"],
    responses={
        409: {"description": "frame_id 已被不同请求体处理（幂等冲突）"},
        422: {"description": "乱序帧：frame_id / timestamp 非严格递增"},
    },
)
def post_frame(session_id: str, frame: FrameIn) -> FrameOut:
    try:
        session = store.get(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    raw = [(d.x, d.y, d.detection_id) for d in frame.detections]
    payload = frame.model_dump()
    try:
        body, _replay = process_frame(
            session,
            payload,
            frame.frame_id,
            frame.timestamp,
            raw,
        )
    except StaleFrameError as exc:
        # 乱序帧：明确拒绝，状态不改变
        raise HTTPException(status_code=422, detail={"error": "stale_frame", "message": str(exc)}) from exc
    except ValueError as exc:
        # 同 frame_id 不同负载：幂等冲突
        raise HTTPException(status_code=409, detail={"error": "frame_payload_conflict", "message": str(exc)}) from exc
    return FrameOut(**body)


@app.delete("/sessions/{session_id}", status_code=204, tags=["sessions"])
def delete_session(session_id: str) -> JSONResponse:
    store.delete(session_id)
    return JSONResponse(status_code=204, content=None)
