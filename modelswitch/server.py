"""Stdlib-only HTTP front end for the model registry.

Endpoints:
    GET  /health           -> 200 {"status": "ok"}
    GET  /version          -> 200 {"version": ...} or 503 if none active
    POST /predict          -> {"x": [..] | [[..], ...]}  =>  {"version", "y"}
    POST /admin/switch     -> {"path": "<artifact dir>"} =>  {"version"} or 409
    POST /admin/rollback   -> 200 {"version"} or 409 if no standby
    GET  /admin/stats      -> registry snapshot (slots, inflight counts)

Run:  python -m modelswitch.server --artifact artifacts/v1 --port 8080
"""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import numpy as np

from .loader import ArtifactError, load_candidate
from .registry import (
    ModelRegistry,
    NoActiveModelError,
    RollbackUnavailableError,
)


class _Handler(BaseHTTPRequestHandler):
    server: "_Server"

    # -- helpers -----------------------------------------------------------

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length))

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter logs
        self.server.log_lines.append(fmt % args)

    # -- routes ------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        if self.path == "/health":
            self._send_json(200, {"status": "ok"})
        elif self.path == "/version":
            version = self.server.registry.current_version()
            if version is None:
                self._send_json(503, {"error": "no model version is active"})
            else:
                self._send_json(200, {"version": version})
        elif self.path == "/admin/stats":
            self._send_json(200, self.server.registry.snapshot())
        else:
            self._send_json(404, {"error": f"unknown path {self.path}"})

    def do_POST(self) -> None:  # noqa: N802 (stdlib naming)
        try:
            body = self._read_json()
        except json.JSONDecodeError as exc:
            self._send_json(400, {"error": f"invalid JSON body: {exc}"})
            return
        if self.path == "/predict":
            self._handle_predict(body)
        elif self.path == "/admin/switch":
            self._handle_switch(body)
        elif self.path == "/admin/rollback":
            self._handle_rollback()
        else:
            self._send_json(404, {"error": f"unknown path {self.path}"})

    # -- handlers ----------------------------------------------------------

    def _handle_predict(self, body: dict[str, Any]) -> None:
        if "x" not in body:
            self._send_json(400, {"error": "request body must contain 'x'"})
            return
        try:
            lease = self.server.registry.acquire()
        except NoActiveModelError as exc:
            self._send_json(503, {"error": str(exc)})
            return
        with lease:
            try:
                y = lease.model.predict(np.asarray(body["x"], dtype=np.float64))
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            self._send_json(200, {"version": lease.version, "y": y.tolist()})

    def _handle_switch(self, body: dict[str, Any]) -> None:
        path = body.get("path")
        if not path:
            self._send_json(400, {"error": "request body must contain 'path'"})
            return
        try:
            candidate = load_candidate(path)
        except ArtifactError as exc:
            # Load/validate/warm-up failed: the active version is untouched.
            self._send_json(
                409,
                {
                    "error": f"{type(exc).__name__}: {exc}",
                    "active_version": self.server.registry.current_version(),
                },
            )
            return
        self.server.registry.switch(candidate)
        self._send_json(200, {"version": candidate.version, "switched": True})

    def _handle_rollback(self) -> None:
        try:
            version = self.server.registry.rollback()
        except RollbackUnavailableError as exc:
            self._send_json(409, {"error": str(exc)})
            return
        self._send_json(200, {"version": version, "rolled_back": True})


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], registry: ModelRegistry) -> None:
        super().__init__(address, _Handler)
        self.registry = registry
        self.log_lines: list[str] = []


def build_server(host: str, port: int, registry: ModelRegistry) -> _Server:
    return _Server((host, port), registry)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="model artifact atomic-switch server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--artifact",
        help="artifact directory to load and activate at startup (optional)",
    )
    args = parser.parse_args(argv)

    registry = ModelRegistry()
    if args.artifact:
        candidate = load_candidate(args.artifact)
        registry.switch(candidate)
        print(f"activated initial version {candidate.version!r} from {args.artifact}")

    server = build_server(args.host, args.port, registry)
    print(f"serving on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
