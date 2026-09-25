"""HTTP front-end for the model-switch service.

Pure standard library (:mod:`http.server`) — no web framework, no network
dependencies.  Run with :mod:`scripts.serve` (see ``scripts/serve.py``).

Endpoints
---------
================  ===========================================================
GET  /health      liveness, never 500s
GET  /status      active version, generation, refcounts, event history
GET  /versions    artifacts present in the registry
POST /switch      {"version": "v2"}  -> load, verify, warm up, atomically
                                       activate; on failure the old version
                                       keeps serving
POST /predict     {"input": [4 floats]} or a batch (N x 4) -> logits
================  ===========================================================

Every JSON response uses the envelope
``{"success": bool, "data": object|null, "error": object|null}``.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import numpy as np

from .errors import ModelSwitchError
from .loader import ArtifactLoader
from .manager import ModelManager
from .model import INPUT_DIM

MAX_BODY_BYTES = 1 << 20  # 1 MiB


def create_manager(registry_root: str, *, initial: str | None = None) -> ModelManager:
    loader = ArtifactLoader(registry_root)
    manager = ModelManager(loader)
    if initial is not None:
        manager.switch_to(initial)
    return manager


def _envelope(data, error: dict | None = None, status: int = 200) -> tuple[int, dict]:
    return status, {"success": error is None, "data": data, "error": error}


class Handler(BaseHTTPRequestHandler):
    # Populated per-server via factory; see build_server.
    manager: ModelManager

    server_version = "ModelSwitch/1.0"

    def log_message(self, fmt: str, *args) -> None:  # noqa: N802 (stdlib name)
        # Compact single-line access log to stderr.
        self.server.access_lock.acquire()  # type: ignore[attr-defined]
        try:
            print(f"{self.address_string()} - {fmt % args}", flush=True)
        finally:
            self.server.access_lock.release()  # type: ignore[attr-defined]

    # ------------------------------------------------------------------ #
    # Response helpers
    # ------------------------------------------------------------------ #

    def _write_json(self, status_code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            # Client went away mid-response; nothing actionable server-side.
            self.close_connection = True

    def _ok(self, data, status: int = 200) -> None:
        code, body = _envelope(data, status=status)
        self._write_json(code, body)

    def _fail(self, status: int, code: str, message: str, extra: dict | None = None) -> None:
        error = {"code": code, "message": message}
        if extra:
            error.update(extra)
        self._write_json(status, _envelope(None, error, status)[1])

    def _read_json(self) -> dict | None:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            self._fail(400, "bad_request", "request body must be a JSON object")
            return None
        if length > MAX_BODY_BYTES:
            # Drain the declared body before replying so the HTTP response
            # is not torn down mid-write (the client is still sending), then
            # close the connection since the oversized request is unusable.
            self.close_connection = True
            remaining = length
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 1 << 16))
                if not chunk:
                    break
                remaining -= len(chunk)
            self._fail(413, "body_too_large", f"body exceeds {MAX_BODY_BYTES} bytes")
            return None
        raw = self.rfile.read(length)
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._fail(400, "bad_json", f"invalid JSON body: {exc}")
            return None
        if not isinstance(obj, dict):
            self._fail(400, "bad_request", "request body must be a JSON object")
            return None
        return obj

    # ------------------------------------------------------------------ #
    # Routing
    # ------------------------------------------------------------------ #

    def do_GET(self) -> None:  # noqa: N802 (stdlib name)
        path = urlparse(self.path).path
        if path == "/health":
            self._ok({"status": "ok"})
            return
        if path == "/status":
            self._ok(self.manager.status())
            return
        if path == "/versions":
            self._ok({"versions": self.manager.available_versions()})
            return
        self._fail(404, "not_found", f"unknown path {path!r}")

    def do_POST(self) -> None:  # noqa: N802 (stdlib name)
        path = urlparse(self.path).path
        if path == "/switch":
            self._handle_switch()
        elif path == "/predict":
            self._handle_predict()
        else:
            self._fail(404, "not_found", f"unknown path {path!r}")

    # ------------------------------------------------------------------ #
    # Endpoint implementations
    # ------------------------------------------------------------------ #

    def _handle_switch(self) -> None:
        body = self._read_json()
        if body is None:
            return
        version = body.get("version")
        if not isinstance(version, str) or not version:
            self._fail(400, "bad_request", "'version' must be a non-empty string")
            return
        try:
            result = self.manager.switch_to(version)
        except ModelSwitchError as exc:
            # Integrity / warm-up / busy / not-found: old version still live.
            # Report the version that continues serving to make rollback
            # observable to the caller.
            self._fail(
                409,
                type(exc).__name__,
                str(exc),
                {"still_serving": self.manager.active_version},
            )
            return
        report = result.load_report
        self._ok(
            {
                "version": result.version,
                "generation": result.generation,
                "replaced": result.replaced,
                "retired_refcount": result.retired_refcount,
                "elapsed_ms": round(result.elapsed_ms, 3),
                "load": {
                    "ok": report.ok,
                    "bytes_verified": report.bytes_verified,
                    "stages": [
                        {"name": t.name, "elapsed_ms": round(t.elapsed_ms, 3)}
                        for t in report.stages
                    ],
                },
            }
        )

    def _handle_predict(self) -> None:
        body = self._read_json()
        if body is None:
            return
        raw = body.get("input")
        try:
            arr = np.asarray(raw, dtype=np.float32)
        except (TypeError, ValueError) as exc:
            self._fail(400, "bad_input", f"input not numeric: {exc}")
            return
        if arr.ndim not in (1, 2) or arr.shape[-1] != INPUT_DIM:
            self._fail(
                400,
                "bad_input",
                f"'input' must be {INPUT_DIM} floats or an N x {INPUT_DIM} batch",
            )
            return
        if not np.isfinite(arr).all():
            self._fail(400, "bad_input", "'input' contains non-finite values")
            return
        try:
            prediction = self.manager.predict(arr)
        except ModelSwitchError as exc:
            self._fail(
                409,
                type(exc).__name__,
                str(exc),
                {"still_serving": self.manager.active_version},
            )
            return
        self._ok(
            {
                "version": prediction.version,
                "generation": prediction.generation,
                "input_shape": list(arr.shape),
                "logits": np.asarray(prediction.logits, dtype=float).tolist(),
            }
        )


def build_server(host: str, port: int, registry_root: str, *, initial: str | None = None) -> ThreadingHTTPServer:
    manager = create_manager(registry_root, initial=initial)

    class _BoundHandler(Handler):
        pass

    _BoundHandler.manager = manager

    server = ThreadingHTTPServer((host, port), _BoundHandler)
    # Serialize interleaved access-log lines from worker threads.
    server.access_lock = threading.Lock()  # type: ignore[attr-defined]
    server.manager = manager  # type: ignore[attr-defined]
    return server
