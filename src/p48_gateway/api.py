"""FastAPI edge for the gateway."""

from __future__ import annotations

import hmac as _hmac
import json
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from .crypto import NonceStore, hmac_verify, request_signing_payload
from .gateway_runtime import GatewayRuntime
from .models import CommandRequest

SSE_BUDGET_SECONDS = 15.0


def _json_error(status: int, code: str, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": code, "detail": detail})


def create_app(runtime: GatewayRuntime) -> FastAPI:
    app = FastAPI(title="P048 multi-robot naming-isolation gateway", version="1.0.0")
    nonces = NonceStore()

    async def authenticate(request: Request, body_obj, robot_id: str):
        """Shared HMAC auth for all tester-facing requests.

        Returns ``(registry, tester)`` on success, or a prebuilt
        ``JSONResponse`` error.  Unknown robots and missing grants yield the
        same 403 so existence cannot be probed.
        """
        header = request.headers.get
        tester_name = header("X-Tester")
        timestamp_raw = header("X-Timestamp")
        nonce = header("X-Nonce")
        signature = header("X-Signature")
        if not all([tester_name, timestamp_raw, nonce, signature]):
            return _json_error(401, "missing_auth", "all auth headers are required")
        try:
            timestamp = float(timestamp_raw)
        except (TypeError, ValueError):
            return _json_error(401, "bad_timestamp", "X-Timestamp must be epoch seconds")

        registry, _, _ = runtime.store.snapshot()
        tester = registry.testers.get(tester_name or "")
        if tester is None or not registry.tester_can_access(tester, robot_id):
            return _json_error(403, "not_authorized", "tester cannot act on this robot")

        ok, why = nonces.check(nonce, timestamp)
        if not ok:
            return _json_error(401, why, "nonce/timestamp check failed")

        payload = request_signing_payload(
            request.method, request.url.path, tester_name, nonce, timestamp, body_obj
        )
        if not hmac_verify(payload, signature, tester.key):
            return _json_error(401, "bad_signature", "HMAC verification failed")
        return registry, tester

    @app.get("/healthz")
    async def healthz():
        _, epoch, _ = runtime.store.snapshot()
        return {"ok": True, "epoch": epoch}

    # -- commands -----------------------------------------------------------
    @app.post("/robots/{robot_id}/commands")
    async def post_command(robot_id: str, request: Request):
        raw = await request.body()
        try:
            body_obj = json.loads(raw.decode("utf-8")) if raw else None
            if not isinstance(body_obj, dict):
                raise ValueError("body must be a JSON object")
            command_in = CommandRequest.model_validate(body_obj)
        except (ValueError, UnicodeDecodeError):
            return _json_error(400, "bad_json", "request body must be a JSON object")
        except ValidationError as exc:
            return JSONResponse(
                status_code=422,
                content={"error": "invalid_command",
                         "detail": exc.errors(include_url=False)},
            )

        authed = await authenticate(request, body_obj, robot_id)
        if not isinstance(authed, tuple):
            return authed  # prebuilt JSONResponse error

        result = runtime.dispatch_command(
            robot_id=robot_id,
            tester_name=request.headers["X-Tester"],
            target=command_in.target,
            seq=command_in.seq,
            command=command_in.command,
            ttl_seconds=command_in.ttl_seconds,
        )
        status = result.pop("http_status")
        return JSONResponse(status_code=status, content=result)

    # -- queries ------------------------------------------------------------
    @app.get("/robots")
    async def list_robots():
        registry, epoch, _ = runtime.store.snapshot()
        return {
            "epoch": epoch,
            "robots": [
                {"robot_id": rid, "namespace": r.namespace}
                for rid, r in sorted(registry.robots.items())
            ],
        }

    @app.get("/robots/{robot_id}/status")
    async def get_status(robot_id: str, request: Request):
        authed = await authenticate(request, None, robot_id)
        if not isinstance(authed, tuple):
            return authed
        status = runtime.robot_status(robot_id)
        if status is None:
            return _json_error(404, "unknown_robot", "robot is not registered")
        return status

    @app.get("/robots/{robot_id}/events")
    async def get_events(robot_id: str, request: Request):
        authed = await authenticate(request, None, robot_id)
        if not isinstance(authed, tuple):
            return authed
        registry, _tester = authed
        if robot_id not in registry.robots:
            return _json_error(404, "unknown_robot", "robot is not registered")
        try:
            limit = min(max(int(request.query_params.get("limit", 50)), 1), 100)
        except ValueError:
            limit = 50
        return {"robot_id": robot_id, "events": runtime.bus.history(robot_id, limit=limit)}

    @app.get("/robots/{robot_id}/events/stream")
    async def stream_events(robot_id: str, request: Request):
        authed = await authenticate(request, None, robot_id)
        if not isinstance(authed, tuple):
            return authed
        registry, _tester = authed
        if robot_id not in registry.robots:
            return _json_error(404, "unknown_robot", "robot is not registered")

        queue = runtime.bus.subscribe()

        def generator():
            yield ": connected\n\n"
            # SSE is short-lived in this implementation: stream real events for
            # the budget then close.  Only this robot's events pass the filter.
            deadline = time.monotonic() + SSE_BUDGET_SECONDS
            while time.monotonic() < deadline:
                try:
                    event = queue.popleft()
                except IndexError:
                    yield ": keepalive\n\n"
                    time.sleep(0.25)
                    continue
                if event.get("robot_id") == robot_id:
                    yield f"event: {event.get('type', 'event')}\n"
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            runtime.bus.unsubscribe(queue)

        return StreamingResponse(generator(), media_type="text/event-stream")

    # -- admin --------------------------------------------------------------
    @app.post("/admin/registry/reload")
    async def reload_registry(request: Request):
        registry, _, _ = runtime.store.snapshot()
        supplied = request.headers.get("X-Admin-Key", "")
        if not _hmac.compare_digest(supplied, registry.admin_key):
            return _json_error(401, "bad_admin_key", "admin authentication required")
        try:
            return runtime.reload_registry()
        except Exception as exc:  # noqa: BLE001 - keep serving the old registry
            return _json_error(400, "invalid_registry", str(exc))

    return app
