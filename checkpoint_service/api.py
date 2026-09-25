"""HTTP API for the checkpoint service (stdlib only, no web framework).

Routes
------
GET    /health
GET    /runs
POST   /runs/{run_id}                 create (JSON body: TrainConfig fields, optional)
POST   /runs/{run_id}/train           resume & train (JSON body: {"stop_after": N}, optional)
GET    /runs/{run_id}/status
GET    /runs/{run_id}/checkpoint      inspect committed checkpoint sections
POST   /runs/{run_id}/inspect         alias for GET checkpoint (for clients that POST)

All responses are JSON. Errors carry a non-2xx status and an "error" field.
"""

from __future__ import annotations

import json
import logging
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .config import TrainConfig
from .errors import (
    CheckpointCorruptError,
    CheckpointFormatError,
    CheckpointNotFoundError,
    DatasetMismatchError,
    InvalidRequestError,
    RunExistsError,
    RunNotFoundError,
    UnsupportedCheckpointVersionError,
)
from .service import TrainingService

logger = logging.getLogger("checkpoint_service.api")

_MAX_BODY_BYTES = 64 * 1024

# Exception -> HTTP status.
_ERROR_STATUS: dict[type[Exception], int] = {
    InvalidRequestError: 400,
    CheckpointFormatError: 422,
    CheckpointCorruptError: 422,
    UnsupportedCheckpointVersionError: 422,
    DatasetMismatchError: 409,
    RunExistsError: 409,
    RunNotFoundError: 404,
    CheckpointNotFoundError: 404,
}


def _json_default(obj: Any) -> Any:
    # numpy scalars/arrays may appear in snapshots.
    try:
        import numpy as np

        if isinstance(obj, np.ndarray):
            return obj.tolist()
        if isinstance(obj, np.generic):
            return obj.item()
    except ImportError:  # pragma: no cover - numpy always present in this project
        pass
    return str(obj)


def make_handler(service: TrainingService) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "CheckpointService/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            logger.info("%s - %s", self.address_string(), fmt % args)

        # ------------------------------------------------------------ #
        # Helpers
        # ------------------------------------------------------------ #

        def _send_json(self, code: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, default=_json_default).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            if length > _MAX_BODY_BYTES:
                raise InvalidRequestError("request body too large")
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise InvalidRequestError(f"invalid JSON body: {exc}") from exc
            if not isinstance(data, dict):
                raise InvalidRequestError("JSON body must be an object")
            return data

        def _guard(self, fn: Callable[[], Any], success: int = 200) -> None:
            try:
                result = fn()
            except Exception as exc:  # map known errors; never leak a traceback
                status = 400
                for exc_type, code in _ERROR_STATUS.items():
                    if isinstance(exc, exc_type):
                        status = code
                        break
                if status == 400 and not isinstance(exc, InvalidRequestError):
                    if isinstance(exc, (ValueError, TypeError)):
                        status = 400
                    else:
                        logger.exception("unhandled error")
                        status = 500
                self._send_json(status, {"error": type(exc).__name__, "detail": str(exc)})
                return
            if result is None:
                result = {"ok": True}
            self._send_json(success, result)

        # ------------------------------------------------------------ #
        # Routing
        # ------------------------------------------------------------ #

        def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
            path = self.path.split("?", 1)[0].rstrip("/") or "/"

            if path == "/health":
                self._guard(lambda: {"status": "ok"})
                return
            if path == "/runs":
                self._guard(service.list_runs)
                return

            m = re.fullmatch(r"/runs/([A-Za-z0-9_\-]+)/(status|checkpoint)", path)
            if m:
                run_id, resource = m.group(1), m.group(2)
                if resource == "status":
                    self._guard(lambda: service.status(run_id))
                else:
                    self._guard(lambda: service.inspect_checkpoint(run_id))
                return

            self._send_json(404, {"error": "NotFound", "detail": f"no route for GET {path}"})

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0].rstrip("/")

            if re.fullmatch(r"/runs/[A-Za-z0-9_\-]+", path):
                run_id = path.rsplit("/", 1)[1]

                def create() -> dict[str, Any]:
                    body = self._read_json()
                    overwrite = bool(body.pop("overwrite", False))
                    cfg = _config_from_body(body)
                    return service.create_run(run_id, cfg, overwrite=overwrite)

                self._guard(create, success=201)
                return

            m = re.fullmatch(r"/runs/([A-Za-z0-9_\-]+)/train", path)
            if m:
                run_id = m.group(1)

                def train() -> dict[str, Any]:
                    body = self._read_json()
                    stop_after = body.get("stop_after")
                    if stop_after is not None and (
                        not isinstance(stop_after, int) or stop_after < 0
                    ):
                        raise InvalidRequestError("stop_after must be a non-negative int")
                    return service.train(run_id, stop_after=stop_after)

                self._guard(train)
                return

            self._send_json(404, {"error": "NotFound", "detail": f"no route for POST {path}"})

    return Handler


def _config_from_body(body: dict[str, Any]) -> TrainConfig:
    if not body:
        return TrainConfig()
    allowed = set(TrainConfig.__dataclass_fields__)  # type: ignore[attr-defined]
    unknown = set(body) - allowed
    if unknown:
        raise InvalidRequestError(f"unknown config fields: {sorted(unknown)}")
    try:
        return TrainConfig(**body)
    except (TypeError, ValueError) as exc:
        raise InvalidRequestError(str(exc)) from exc


def build_server(host: str, port: int, base_dir: str) -> ThreadingHTTPServer:
    service = TrainingService(base_dir)
    return ThreadingHTTPServer((host, port), make_handler(service))


def serve(host: str = "127.0.0.1", port: int = 8080, base_dir: str = "./runs") -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    httpd = build_server(host, port, base_dir)
    logger.info("checkpoint service listening on http://%s:%s (runs dir: %s)", host, port, base_dir)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("shutting down")
    finally:
        httpd.server_close()
