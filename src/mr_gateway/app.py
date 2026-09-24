"""FastAPI application: the multi-robot command gateway.

Request pipeline for ``POST /robots/{rid}/commands``, strictly ordered:

  1. robot must be registered (registry is the namespace authority)
  2. bearer token signature / expiry / robot-binding / mapping epoch
  3. command test identity must match the token's test identity
  4. target topic must be a relative name strictly inside the namespace
  5. command validity window must not already have elapsed
  6. per-target sequence must not roll back or repeat
  7. publish the JSON envelope to the validated ROS FQN (latched)

Every rejection is audited with its reason and emitted on the event bus;
accepted commands are audited too.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import auth
from .audit import AuditLog
from .config import Settings
from .events import EventBus
from .models import (
    CommandRequest,
    RegisterRequest,
    IssueTokenRequest,
)
from .naming import NamingError, qualify_topic, validate_robot_id
from .registry import (
    Registry,
    UnknownRobot,
    SEQ_ACCEPTED,
    SEQ_DUPLICATE,
    SEQ_ROLLBACK,
)
from .ros_bridge import RosBridge

ENVELOPE_VERSION = 1
SSE_HEARTBEAT_SECONDS = 15.0


# --------------------------------------------------------------------------- #
# Application state
# --------------------------------------------------------------------------- #


class State:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.registry = Registry()
        self.bus = EventBus()
        self.audit = AuditLog(settings.audit_log_path)
        self.bridge = RosBridge(self.registry)
        self.counters = {"published": 0, "rejected": 0}
        # Serializes mapping changes so epoch bumps + stream closures + ROS
        # publisher disposal happen as one observable step.
        self.map_lock = asyncio.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    state: State = app.state.gw
    state.bridge.start()
    try:
        yield
    finally:
        state.bridge.stop()
        state.audit.close()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    app = FastAPI(
        title="Multi-Robot Command Gateway",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.gw = State(settings)
    _register_routes(app)
    return app


def _state(request: Request) -> State:
    return request.app.state.gw


# --------------------------------------------------------------------------- #
# Auth dependencies
# --------------------------------------------------------------------------- #


def require_admin(
    request: Request,
    x_admin_token: str | None = Header(default=None),
) -> None:
    settings: Settings = request.app.state.gw.settings
    if not x_admin_token or x_admin_token != settings.admin_token:
        raise HTTPException(status_code=401, detail={
            "error": "unauthorized", "reason": "admin_token_invalid"})


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail={
            "error": "unauthorized",
            "reason": "missing_bearer_token",
            "detail": "send 'Authorization: Bearer <token>'"})
    return authorization[len("Bearer "):].strip()


def authorize_robot(state: State, robot_id: str, token: str) -> auth.Claims:
    """Verify a robot command/event token against the live mapping epoch."""
    try:
        robot = state.registry.get(robot_id)
    except UnknownRobot:
        raise HTTPException(status_code=404, detail={
            "error": "unknown_robot",
            "reason": "robot_not_registered",
            "detail": {"robot_id": robot_id}})
    try:
        return auth.verify_token(
            token, state.settings.hmac_secret, robot_id, robot.epoch)
    except auth.TokenStale:
        raise HTTPException(status_code=401, detail={
            "error": "unauthorized",
            "reason": "stale_authorization",
            "detail": {
                "robot_id": robot_id,
                "current_epoch": robot.epoch,
                "hint": "mapping changed; obtain a new token"}})
    except auth.TokenExpired:
        raise HTTPException(status_code=401, detail={
            "error": "unauthorized", "reason": "token_expired"})
    except auth.TokenSignatureInvalid:
        raise HTTPException(status_code=401, detail={
            "error": "unauthorized", "reason": "token_signature_invalid"})
    except auth.TokenMalformed:
        raise HTTPException(status_code=401, detail={
            "error": "unauthorized", "reason": "token_malformed"})


# --------------------------------------------------------------------------- #
# Route registration
# --------------------------------------------------------------------------- #


def _register_routes(app: FastAPI) -> None:

    @app.exception_handler(NamingError)
    async def naming_handler(_request: Request, exc: NamingError):
        return JSONResponse(status_code=400, content={
            "error": "bad_topic", "reason": str(exc)})

    # -- health / introspection -------------------------------------------- #

    @app.get("/health")
    async def health(request: Request) -> dict[str, Any]:
        state = _state(request)
        return {
            "status": "ok",
            "service": "mr-gateway",
            "robots": len(state.registry.list()),
            "counters": state.counters,
            "ros": state.bridge.status(),
        }

    # -- admin: registry ---------------------------------------------------- #

    @app.post("/admin/robots/{robot_id}",
              dependencies=[Depends(require_admin)])
    async def register_robot(robot_id: str, body: RegisterRequest,
                             request: Request) -> dict[str, Any]:
        state = _state(request)
        # Validate the robot id up front (NamingError -> 400 handler).
        validate_robot_id(robot_id)

        async with state.map_lock:
            old = None
            if state.registry.exists(robot_id):
                old = state.registry.get(robot_id)
            robot = state.registry.register(robot_id, body.namespace)
            change = "created" if old is None else (
                "remapped" if old.namespace != robot.namespace
                else "refreshed")

            # Old authorizations for this robot are dead the instant the
            # mapping changes: close live event streams and destroy any
            # publishers we held in the old (possibly now foreign) namespace.
            if change == "remapped":
                await state.bus.close_robot(
                    robot_id, f"namespace remapped {old.namespace} -> "
                              f"{robot.namespace}")
                disposed = state.bridge.dispose_namespace(old.namespace)
            else:
                disposed = 0

            state.audit.record(
                "mapping_changed", robot_id=robot_id, change=change,
                old_namespace=None if old is None else old.namespace,
                new_namespace=robot.namespace, epoch=robot.epoch,
                publishers_disposed=disposed)
            await state.bus.emit(robot_id, {
                "type": "mapping_changed", "change": change,
                "namespace": robot.namespace, "epoch": robot.epoch})

        # Warm latched publishers outside the mapping lock. Every topic goes
        # through the same qualify_topic chokepoint, so prewarm can never
        # create a publisher outside the registered namespace.
        warmed = []
        for topic in body.prewarm_topics:
            fqn = qualify_topic(robot.namespace, topic)
            state.bridge.prewarm(fqn)
            warmed.append(fqn)

        return {
            "robot_id": robot.robot_id,
            "namespace": robot.namespace,
            "epoch": robot.epoch,
            "change": change,
            "prewarmed": warmed,
        }

    @app.delete("/admin/robots/{robot_id}",
                dependencies=[Depends(require_admin)])
    async def remove_robot(robot_id: str, request: Request) -> dict[str, Any]:
        state = _state(request)
        async with state.map_lock:
            try:
                old = state.registry.get(robot_id)
            except UnknownRobot:
                raise HTTPException(status_code=404, detail={
                    "error": "unknown_robot",
                    "reason": "robot_not_registered"})
            state.registry.remove(robot_id)
            await state.bus.close_robot(
                robot_id, "robot deregistered; all authorizations revoked")
            disposed = state.bridge.dispose_namespace(old.namespace)
            state.audit.record(
                "mapping_changed", robot_id=robot_id, change="removed",
                old_namespace=old.namespace, new_namespace=None,
                publishers_disposed=disposed)
        return {"robot_id": robot_id, "removed": True,
                "publishers_disposed": disposed}

    @app.get("/admin/robots", dependencies=[Depends(require_admin)])
    async def list_robots(request: Request) -> dict[str, Any]:
        state = _state(request)
        return {"robots": [
            {
                "robot_id": r.robot_id,
                "namespace": r.namespace,
                "epoch": r.epoch,
                "watermarks": r.watermarks,
                "created_at": r.created_at,
                "updated_at": r.updated_at,
            } for r in state.registry.list()
        ]}

    # -- admin: token issuance --------------------------------------------- #

    @app.post("/admin/robots/{robot_id}/tokens",
              dependencies=[Depends(require_admin)])
    async def issue_token(robot_id: str, body: IssueTokenRequest,
                          request: Request) -> dict[str, Any]:
        state = _state(request)
        try:
            robot = state.registry.get(robot_id)
        except UnknownRobot:
            raise HTTPException(status_code=404, detail={
                "error": "unknown_robot",
                "reason": "robot_not_registered"})
        ttl = body.ttl_seconds or state.settings.default_ttl_seconds
        token = auth.issue_token(
            state.settings.hmac_secret, robot_id, body.tester_id,
            robot.epoch, ttl)
        state.audit.record("token_issued", robot_id=robot_id,
                           tester_id=body.tester_id, epoch=robot.epoch,
                           ttl_seconds=ttl)
        return {
            "token": token,
            "robot_id": robot_id,
            "tester_id": body.tester_id,
            "epoch": robot.epoch,
            "expires_at": int(time.time() + ttl),
        }

    # -- robot: command submission ----------------------------------------- #

    @app.post("/robots/{robot_id}/commands")
    async def submit_command(robot_id: str, body: CommandRequest,
                             request: Request,
                             authorization: str | None = Header(default=None)):
        state = _state(request)

        # 1+2. registration & token (raises 404/401)
        claims = authorize_robot(state, robot_id, _bearer(authorization))

        # 3. test identity binding
        if body.tester_id is not None and body.tester_id != claims.tester_id:
            return _reject(
                state, robot_id, claims, 403, "tester_mismatch",
                f"command tester_id {body.tester_id!r} does not match token "
                f"identity {claims.tester_id!r}",
                target=body.target, sequence=body.sequence)

        # 4. namespace resolution + topic safety (NamingError -> 400)
        try:
            fqn, robot = state.registry.fqn(robot_id, body.target)
        except UnknownRobot:
            raise HTTPException(status_code=404, detail={
                "error": "unknown_robot",
                "reason": "robot_not_registered"})

        # 5. validity window
        now = time.time()
        expires_at = _resolve_expiry(body, now)
        if expires_at is None:
            return _reject(
                state, robot_id, claims, 400, "validity_missing",
                "provide ttl_seconds or expires_at",
                target=body.target, sequence=body.sequence)
        if isinstance(expires_at, tuple) and expires_at[0] == "conflict":
            return _reject(
                state, robot_id, claims, 400, "validity_conflict",
                "ttl_seconds and expires_at disagree",
                target=body.target, sequence=body.sequence,
                detail=expires_at[1])
        if expires_at <= now:
            return _reject(
                state, robot_id, claims, 400, "command_expired",
                f"command expired at {expires_at:.3f}; now {now:.3f}",
                target=body.target, sequence=body.sequence,
                detail={"expires_at": expires_at, "now": now})

        # 6. monotonic sequence per (robot, target)
        seq_result = state.registry.accept_sequence(
            robot_id, body.target, body.sequence)
        if seq_result.status == SEQ_ROLLBACK:
            return _reject(
                state, robot_id, claims, 409, "sequence_rollback",
                f"sequence {body.sequence} is below high-water mark "
                f"{seq_result.previous} for {body.target!r}",
                target=body.target, sequence=body.sequence,
                detail={"high_water": seq_result.previous})
        if seq_result.status == SEQ_DUPLICATE:
            return _reject(
                state, robot_id, claims, 409, "sequence_duplicate",
                f"sequence {body.sequence} was already accepted for "
                f"{body.target!r}",
                target=body.target, sequence=body.sequence,
                detail={"high_water": seq_result.previous})

        # 7. real ROS 2 publish (latched, reliable) inside the namespace
        envelope = {
            "v": ENVELOPE_VERSION,
            "rid": robot_id,
            "ns": robot.namespace,
            "target": body.target,
            "seq": body.sequence,
            "tester_id": claims.tester_id,
            "issued_at": now,
            "expires_at": expires_at,
            "payload": body.payload,
        }
        info = state.bridge.publish(fqn, envelope)

        state.counters["published"] += 1
        state.audit.record(
            "command_published", robot_id=robot_id,
            namespace=robot.namespace, topic=fqn,
            sequence=body.sequence, tester_id=claims.tester_id,
            epoch=claims.epoch, expires_at=expires_at,
            subscriber_count=info["subscriber_count"],
            payload=body.payload)
        await state.bus.emit(robot_id, {
            "type": "command_published",
            "namespace": robot.namespace, "topic": fqn,
            "sequence": body.sequence, "tester_id": claims.tester_id,
            "expires_at": expires_at,
            "subscriber_count": info["subscriber_count"]})

        return {
            "status": "published",
            "robot_id": robot_id,
            "namespace": robot.namespace,
            "topic": fqn,
            "sequence": body.sequence,
            "epoch": claims.epoch,
            "tester_id": claims.tester_id,
            "expires_at": expires_at,
            "subscriber_count": info["subscriber_count"],
            "published_at": info["published_at"],
        }

    # -- robot: isolated query --------------------------------------------- #

    @app.get("/robots/{robot_id}/state")
    async def robot_state(robot_id: str, request: Request,
                          authorization: str | None = Header(default=None)):
        state = _state(request)
        claims = authorize_robot(state, robot_id, _bearer(authorization))
        robot = state.registry.get(robot_id)
        # Only the caller's own namespace/state is visible. There is no list
        # endpoint on the robot side of the API.
        return {
            "robot_id": robot.robot_id,
            "namespace": robot.namespace,
            "epoch": robot.epoch,
            "watermarks": robot.watermarks,
            "tester_id": claims.tester_id,
        }

    # -- robot: isolated SSE event stream ---------------------------------- #

    @app.get("/robots/{robot_id}/events")
    async def robot_events(robot_id: str, request: Request,
                           authorization: str | None = Header(default=None)):
        state = _state(request)
        claims = authorize_robot(state, robot_id, _bearer(authorization))

        queue = await state.bus.subscribe(robot_id)

        async def stream():
            try:
                yield _sse("ready", {
                    "robot_id": robot_id, "tester_id": claims.tester_id,
                    "epoch": claims.epoch,
                    "namespace": state.registry.get(robot_id).namespace})
                while True:
                    # Wait for an event or the heartbeat deadline. Disconnect
                    # is only consulted between events (not as a gate at the
                    # top of the loop) so a server that has not yet sent
                    # http.disconnect cannot prematurely end a live stream.
                    try:
                        event = await asyncio.wait_for(
                            queue.get(), timeout=SSE_HEARTBEAT_SECONDS)
                    except asyncio.TimeoutError:
                        if await request.is_disconnected():
                            break
                        yield b": keepalive\n\n"
                        continue
                    if await request.is_disconnected():
                        break
                    terminal = event.get("terminal", False)
                    yield _sse(event.get("type", "event"), event)
                    if terminal:
                        break
            finally:
                await state.bus.unsubscribe(robot_id, queue)

        return StreamingResponse(
            stream(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache",
                     "X-Accel-Buffering": "no"})

    # -- admin SSE (global view) ------------------------------------------- #

    @app.get("/admin/events")
    async def admin_events(request: Request,
                           x_admin_token: str | None = Header(default=None),
                           token: str | None = None):
        state = _state(request)
        # Header is preferred; ?token= is allowed for plain curl/EventSource.
        if x_admin_token != state.settings.admin_token and \
                token != state.settings.admin_token:
            raise HTTPException(status_code=401, detail={
                "error": "unauthorized", "reason": "admin_token_invalid"})
        queue = await state.bus.subscribe_admin()

        async def stream():
            try:
                yield _sse("ready", {"view": "admin"})
                while True:
                    try:
                        event = await asyncio.wait_for(
                            queue.get(), timeout=SSE_HEARTBEAT_SECONDS)
                    except asyncio.TimeoutError:
                        if await request.is_disconnected():
                            break
                        yield b": keepalive\n\n"
                        continue
                    if await request.is_disconnected():
                        break
                    yield _sse(event.get("type", "event"), event)
            finally:
                await state.bus.unsubscribe_admin(queue)

        return StreamingResponse(
            stream(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache",
                     "X-Accel-Buffering": "no"})


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _sse(event_type: str, data: dict[str, Any]) -> bytes:
    payload = json.dumps(data, separators=(",", ":"), sort_keys=True,
                         ensure_ascii=False)
    return f"event: {event_type}\ndata: {payload}\n\n".encode("utf-8")


def _resolve_expiry(body: CommandRequest, now: float):
    """Return absolute expiry, None if unspecified, ('conflict', info) if
    ttl and absolute expiry disagree beyond tolerance."""
    if body.ttl_seconds is None and body.expires_at is None:
        return None
    if body.ttl_seconds is not None:
        exp_from_ttl = now + body.ttl_seconds
        if body.expires_at is not None and \
                abs(exp_from_ttl - body.expires_at) > 2.0:
            return ("conflict",
                    {"from_ttl": exp_from_ttl,
                     "expires_at": body.expires_at})
        return exp_from_ttl
    return float(body.expires_at)


def _reject(state: State, robot_id: str, claims: "auth.Claims | None",
            status_code: int, reason: str, message: str, *,
            target: str | None = None, sequence: int | None = None,
            detail: dict[str, Any] | None = None) -> JSONResponse:
    state.counters["rejected"] += 1
    state.audit.record(
        "command_rejected", robot_id=robot_id,
        tester_id=None if claims is None else claims.tester_id,
        epoch=None if claims is None else claims.epoch,
        target=target, sequence=sequence, reason=reason, message=message,
        **(detail or {}))
    # Best-effort live notification; emit() itself never raises to callers.
    if state.registry.exists(robot_id):
        event = {"type": "command_rejected", "reason": reason,
                 "message": message, "target": target, "sequence": sequence}
        if detail:
            event["detail"] = detail
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(state.bus.emit(robot_id, event))
        except RuntimeError:
            pass
    return JSONResponse(status_code=status_code, content={
        "error": "command_rejected", "reason": reason, "message": message,
        "detail": detail or {}})
