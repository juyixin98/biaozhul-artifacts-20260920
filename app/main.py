"""FastAPI application: ROS2 bag replay control service.

Run (ROS must be sourced for rosbag2_py)::

    source /opt/ros/jazzy/setup.bash
    uvicorn app.main:app --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any, Iterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import bagstore
from .bagstore import BagAccessError, BagNotFoundError, CorruptBagError
from .checkpoints import CheckpointSignatureError, SourceChangedError
from .config import Settings, settings as default_settings
from .crypto import load_or_create_secret
from .models import (
    CheckpointRequest,
    CreateSessionRequest,
    FilterRequest,
    RateRequest,
    RestoreRequest,
    SeekRequest,
)
from .sessions import Session, SessionManager


def _rosbag_available() -> bool:
    try:
        import rosbag2_py  # noqa: F401
    except Exception:
        return False
    return True


def create_app(app_settings: Settings) -> FastAPI:
    """Build an app bound to explicit settings (tests and launch use this)."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> Iterator[None]:
        key = load_or_create_secret(app_settings.secret_file)
        _app.state.settings = app_settings
        _app.state.manager = SessionManager(app_settings, key)
        try:
            yield
        finally:
            _app.state.manager.shutdown_all()

    app = FastAPI(
        title="ROS Replay Checkpoint Service",
        version="1.0.0",
        description=(
            "Replays local ROS2 bags on a timestamp-driven virtual clock with "
            "stable sequence numbers, pause/speed/seek, generation-based "
            "cancellation and HMAC-sealed checkpoints."
        ),
        lifespan=lifespan,
    )

    def manager_of(request: Request) -> SessionManager:
        return request.app.state.manager

    def require_session(request: Request, session_id: str) -> Session:
        try:
            return request.app.state.manager.get(session_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="session not found")

    def state_response(session: Session) -> dict[str, Any]:
        return {"session_id": session.id, "state": session.engine.snapshot()}

    # --------------------------------------------------------------- errors

    @app.exception_handler(CorruptBagError)
    async def _corrupt_handler(_r: Request, exc: CorruptBagError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"detail": {"error": "corrupt_bag", "message": str(exc)}},
        )

    @app.exception_handler(BagNotFoundError)
    async def _not_found_handler(_r: Request, exc: BagNotFoundError) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content={"detail": {"error": "bag_not_found", "message": str(exc)}},
        )

    @app.exception_handler(BagAccessError)
    async def _access_handler(_r: Request, exc: BagAccessError) -> JSONResponse:
        return JSONResponse(
            status_code=403,
            content={"detail": {"error": "bag_access_denied", "message": str(exc)}},
        )

    @app.exception_handler(SourceChangedError)
    async def _source_handler(_r: Request, exc: SourceChangedError) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={"detail": {"error": "source_changed", "message": str(exc)}},
        )

    @app.exception_handler(CheckpointSignatureError)
    async def _sig_handler(_r: Request, exc: CheckpointSignatureError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={
                "detail": {"error": "bad_checkpoint_signature", "message": str(exc)}
            },
        )

    # --------------------------------------------------------------- health

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "rosbag2_py": _rosbag_available(),
            "transport": app_settings.transport,
        }

    @app.get("/bags")
    def list_bags() -> dict[str, Any]:
        bags: list[dict[str, Any]] = []
        seen: set[str] = set()
        for root in app_settings.bag_roots:
            if not root.is_dir():
                continue
            for child in sorted(root.rglob("metadata.yaml")):
                bag_dir = child.parent.resolve()
                key = str(bag_dir)
                if key in seen:
                    continue
                seen.add(key)
                try:
                    storage_id = bagstore._detect_storage_id(bag_dir)
                    info = bagstore.rosbag2_py.Info().read_metadata(key, storage_id)
                    bags.append(
                        {
                            "uri": key,
                            "storage_id": storage_id,
                            "message_count": info.message_count,
                            "duration_ns": info.duration.nanoseconds,
                            "topics": [
                                t.topic_metadata.name
                                for t in info.topics_with_message_count
                            ],
                        }
                    )
                except Exception as exc:  # noqa: BLE001
                    bags.append({"uri": key, "error": str(exc)})
        return {"roots": [str(r) for r in app_settings.bag_roots], "bags": bags}

    @app.get("/bags/inspect")
    def inspect_bag(uri: str) -> dict[str, Any]:
        bag_dir = bagstore.resolve_bag_uri(uri, app_settings.bag_roots)
        index, _payloads = bagstore.index_bag_with_payloads(bag_dir)
        return index.describe()

    # ------------------------------------------------------------- sessions

    @app.post("/sessions", status_code=201)
    def create_session(request: Request, req: CreateSessionRequest) -> dict[str, Any]:
        try:
            session = manager_of(request).create(
                req.bag_uri,
                topics=req.topics,
                rate=req.rate,
                autoplay=req.autoplay,
                transport=req.transport,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {
            "session_id": session.id,
            "transport": session.transport,
            "bag": session.index.describe(),
            "state": session.engine.snapshot(),
        }

    @app.get("/sessions")
    def list_sessions(request: Request) -> dict[str, Any]:
        return {"sessions": manager_of(request).list()}

    @app.get("/sessions/{session_id}")
    def get_session(request: Request, session_id: str) -> dict[str, Any]:
        session = require_session(request, session_id)
        return {
            "session_id": session.id,
            "transport": session.transport,
            "bag": session.index.describe(),
            "state": session.engine.snapshot(),
        }

    @app.delete("/sessions/{session_id}", status_code=204)
    def delete_session(request: Request, session_id: str) -> None:
        try:
            manager_of(request).delete(session_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="session not found")

    @app.post("/sessions/{session_id}/play")
    def play(request: Request, session_id: str) -> dict[str, Any]:
        session = require_session(request, session_id)
        session.engine.play()
        return state_response(session)

    @app.post("/sessions/{session_id}/pause")
    def pause(request: Request, session_id: str) -> dict[str, Any]:
        session = require_session(request, session_id)
        session.engine.pause()
        return state_response(session)

    @app.post("/sessions/{session_id}/stop")
    def stop(request: Request, session_id: str) -> dict[str, Any]:
        session = require_session(request, session_id)
        session.engine.stop()
        return state_response(session)

    @app.post("/sessions/{session_id}/rate")
    def rate(request: Request, session_id: str, req: RateRequest) -> dict[str, Any]:
        session = require_session(request, session_id)
        try:
            session.engine.set_rate(req.rate)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return state_response(session)

    @app.post("/sessions/{session_id}/seek")
    def seek(request: Request, session_id: str, req: SeekRequest) -> dict[str, Any]:
        session = require_session(request, session_id)
        try:
            session.engine.seek(
                seq=req.seq,
                timestamp_ns=req.timestamp_ns,
                ratio=req.ratio,
                play_after=req.play_after,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return state_response(session)

    @app.post("/sessions/{session_id}/filter")
    def filter_topics(request: Request, session_id: str, req: FilterRequest) -> dict[str, Any]:
        session = require_session(request, session_id)
        try:
            session.engine.set_topic_filter(req.topics)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return state_response(session)

    # ---------------------------------------------------------- published IO

    @app.get("/sessions/{session_id}/stream")
    async def stream(request: Request, session_id: str) -> StreamingResponse:
        """NDJSON stream of messages + control events (loopback transport)."""
        session = require_session(request, session_id)
        if session.transport != "loopback":
            raise HTTPException(
                status_code=400,
                detail="stream endpoint requires transport=loopback; "
                "subscribe to the DDS topics for transport=ros",
            )
        cid, q = session.sink.subscribe()

        async def gen() -> Any:
            try:
                loop = asyncio.get_running_loop()
                while True:
                    item = await loop.run_in_executor(None, q.get)
                    if item is None:
                        break
                    yield (
                        json.dumps(item, separators=(",", ":"), ensure_ascii=False)
                        + "\n"
                    ).encode()
            finally:
                session.sink.unsubscribe(cid)

        return StreamingResponse(gen(), media_type="application/x-ndjson")

    @app.get("/sessions/{session_id}/messages")
    def recent_messages(
        request: Request,
        session_id: str,
        after_history_id: int = 0,
        limit: int = 1000,
    ) -> dict[str, Any]:
        session = require_session(request, session_id)
        limit = max(1, min(limit, 5000))
        items = session.sink.history(after_history_id=after_history_id, limit=limit)
        return {
            "tail_history_id": session.sink.history_tail_id(),
            "count": len(items),
            "items": items,
        }

    # ----------------------------------------------------------- checkpoints

    @app.post("/sessions/{session_id}/checkpoint")
    def save_checkpoint(
        request: Request, session_id: str, req: CheckpointRequest
    ) -> dict[str, Any]:
        session = require_session(request, session_id)
        result = manager_of(request).save_checkpoint(session, pause=req.pause)
        return {
            "checkpoint_id": result["checkpoint_id"],
            "path": result["path"],
            "state": session.engine.snapshot(),
        }

    @app.get("/sessions/{session_id}/checkpoint/{checkpoint_id}")
    def get_checkpoint(
        request: Request, session_id: str, checkpoint_id: str
    ) -> JSONResponse:
        session = require_session(request, session_id)
        try:
            payload = session.checkpoint_store.load_file_verified(checkpoint_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="checkpoint not found")
        return JSONResponse(payload)

    @app.post("/restore")
    def restore(request: Request, req: RestoreRequest) -> dict[str, Any]:
        manager = manager_of(request)
        source: dict[str, Any] | str
        if req.envelope is not None:
            source = req.envelope
        elif req.checkpoint_path:
            source = req.checkpoint_path
        else:
            cp_id = req.checkpoint_id
            candidates = [app_settings.state_dir / f"{cp_id}.json"]
            candidates.extend(app_settings.state_dir.glob(f"*/{cp_id}.json"))
            found = next((p for p in candidates if p.is_file()), None)
            if found is None:
                raise HTTPException(status_code=404, detail="checkpoint not found")
            source = str(found)
        try:
            session = manager.restore(
                source, transport=req.transport, autoplay=req.autoplay
            )
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="checkpoint file not found")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {
            "session_id": session.id,
            "transport": session.transport,
            "restored": True,
            "state": session.engine.snapshot(),
            "bag": session.index.describe(),
        }

    return app


app = create_app(default_settings)
