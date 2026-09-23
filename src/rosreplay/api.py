"""FastAPI application: the HTTP control plane for replay sessions."""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .bagio import BagError
from .checkpoints import CheckpointError
from .config import Settings, get_settings
from .engine import PlayState
from .sessions import SessionError, SessionManager

# A single manager for the process. Tests construct their own app via
# create_app with custom settings.
_manager: SessionManager | None = None


def get_manager() -> SessionManager:
    global _manager
    if _manager is None:
        _manager = SessionManager(get_settings())
    return _manager


class BagRequest(BaseModel):
    uri: str = Field(..., description="Bag path relative to BAG_ROOT, or absolute inside it")


class CreateSessionRequest(BaseModel):
    uri: str
    topics: list[str] | None = None
    rate: float = Field(default=1.0, gt=0)
    play: bool = True


class SeekRequest(BaseModel):
    seq: int | None = Field(default=None, ge=0, description="filtered index position")
    timestamp_ns: int | None = Field(default=None, ge=0)
    play: bool | None = None


class RateRequest(BaseModel):
    rate: float = Field(..., gt=0)


class CheckpointRequest(BaseModel):
    checkpoint_id: str | None = None


class RestoreRequest(BaseModel):
    checkpoint_id: str
    play: bool | None = None


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(
        title="ROS Replay Checkpoint Service",
        version="1.0.0",
        description="Back-end service for deterministic, controllable ROS2 bag replay.",
    )
    manager = SessionManager(settings or get_settings())
    app.state.manager = manager

    @app.exception_handler(BagError)
    async def _bag_error_handler(_request: Any, exc: BagError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"error": "bag_error", "detail": str(exc)})

    @app.exception_handler(SessionError)
    async def _session_error_handler(_request: Any, exc: SessionError) -> JSONResponse:
        status = 404 if "not found" in str(exc) else 400
        return JSONResponse(status_code=status, content={"error": "session_error", "detail": str(exc)})

    @app.exception_handler(CheckpointError)
    async def _cp_error_handler(_request: Any, exc: CheckpointError) -> JSONResponse:
        status = 404 if "not found" in str(exc) else 409
        return JSONResponse(status_code=status, content={"error": "checkpoint_error", "detail": str(exc)})

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # ---------------------------------------------------------------- bags
    @app.post("/bags/info")
    def bags_info(req: BagRequest) -> dict[str, Any]:
        try:
            return manager.describe_bag(req.uri)
        except BagError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ------------------------------------------------------------- sessions
    @app.post("/sessions")
    def create_session(req: CreateSessionRequest) -> dict[str, Any]:
        session = manager.create_session(
            uri=req.uri,
            topics=req.topics,
            rate=req.rate,
            auto_play=req.play,
        )
        return {
            "session_id": session.id,
            "ros_publishing": session.ros is not None,
            "status": session.engine.status(),
        }

    @app.get("/sessions/{session_id}/status")
    def session_status(session_id: str) -> dict[str, Any]:
        session = manager.get(session_id)
        st = session.engine.status()
        st["ros_publishing"] = session.ros is not None
        if session.ros is not None:
            st["dds_topics"] = session.ros.target_topics()
        return st

    @app.post("/sessions/{session_id}/pause")
    def pause(session_id: str) -> dict[str, Any]:
        session = manager.get(session_id)
        session.engine.pause()
        return session.engine.status()

    @app.post("/sessions/{session_id}/resume")
    def resume(session_id: str) -> dict[str, Any]:
        session = manager.get(session_id)
        session.engine.resume()
        return session.engine.status()

    @app.post("/sessions/{session_id}/rate")
    def set_rate(session_id: str, req: RateRequest) -> dict[str, Any]:
        session = manager.get(session_id)
        session.engine.set_rate(req.rate)
        return session.engine.status()

    @app.post("/sessions/{session_id}/seek")
    def seek(session_id: str, req: SeekRequest) -> dict[str, Any]:
        if req.seq is None and req.timestamp_ns is None:
            raise HTTPException(status_code=422, detail="provide seq or timestamp_ns")
        session = manager.get(session_id)
        try:
            result = session.engine.seek(
                target_seq=req.seq, target_ns=req.timestamp_ns, play=req.play
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return result

    @app.get("/sessions/{session_id}/published")
    def published(
        session_id: str,
        generation: int | None = None,
        since_seq: int | None = None,
    ) -> dict[str, Any]:
        session = manager.get(session_id)
        records = session.ring.snapshot(generation=generation, since_seq=since_seq)
        return {
            "count": len(records),
            "records": [
                {
                    "generation": r.generation,
                    "seq": r.seq,
                    "topic": r.topic,
                    "msg_type": r.msg_type,
                    "bag_timestamp_ns": r.bag_timestamp_ns,
                    "wall_published_ns": r.wall_published_ns,
                    "data_len": r.data_len,
                }
                for r in records
            ],
        }

    @app.delete("/sessions/{session_id}")
    def delete_session(session_id: str) -> dict[str, str]:
        manager.close_session(session_id)
        return {"status": "deleted", "session_id": session_id}

    # ---------------------------------------------------------- checkpoints
    @app.post("/sessions/{session_id}/checkpoints")
    def save_checkpoint(session_id: str, req: CheckpointRequest) -> dict[str, Any]:
        cp = manager.save_checkpoint(session_id, req.checkpoint_id)
        return cp.to_dict()

    @app.post("/checkpoints/restore")
    def restore_checkpoint(req: RestoreRequest) -> dict[str, Any]:
        session = manager.restore_checkpoint(req.checkpoint_id, auto_play=req.play)
        return {
            "session_id": session.id,
            "restored_from": req.checkpoint_id,
            "ros_publishing": session.ros is not None,
            "status": session.engine.status(),
        }

    @app.get("/checkpoints/{checkpoint_id}")
    def get_checkpoint(checkpoint_id: str) -> dict[str, Any]:
        return manager.store.load(checkpoint_id).to_dict()

    @app.get("/checkpoints")
    def list_checkpoints() -> dict[str, Any]:
        return {"checkpoints": manager.store.list_all()}

    @app.on_event("shutdown")
    def _shutdown() -> None:
        manager.shutdown_all()

    return app


app = create_app()
