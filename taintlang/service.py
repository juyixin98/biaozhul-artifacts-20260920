"""Zero-dependency JSON/HTTP service.

Endpoints
---------
GET  /health -> {"status": "ok", "version": ...}

POST /analyze
    request:
      {
        "source": "<TaintLang source code>",      # required
        "config": { ... },                        # optional, see Config
        "include_ir": true                        # optional, default true
      }
    response 200:
      { "ok": true,  "result": { ... } }
    response 400 (well-formed request, bad program):
      { "ok": false, "error": { "type", "message", "span"? } }
    response 422 (malformed request / invalid JSON): same error envelope.

The analysis itself never shells out and uses only the Python standard
library (``http.server``); no web framework, no third-party parser.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import __version__
from .config import Config
from .errors import TaintLangError
from .pipeline import analyze_source

MAX_BODY_BYTES = 1_000_000


def run_analysis_payload(payload: Any) -> dict:
    """Pure logic shared by the HTTP handler and tests.

    Returns the result dict; raises TaintLangError for program errors and
    ValueError for malformed requests.
    """
    if not isinstance(payload, dict):
        raise ValueError("request body must be a JSON object")
    source = payload.get("source")
    if not isinstance(source, str):
        raise ValueError("field 'source' is required and must be a string")
    try:
        config = Config.from_dict(payload.get("config"))
    except TaintLangError:
        raise
    include_ir = bool(payload.get("include_ir", True))
    tainted_entry_params = bool(payload.get("tainted_entry_params", True))
    result = analyze_source(source, config=config,
                            tainted_entry_params=tainted_entry_params)
    if not include_ir:
        result.pop("ir", None)
    return result


class _Handler(BaseHTTPRequestHandler):
    server_version = f"TaintLang/{__version__}"

    def log_message(self, fmt, *args):  # quiet by default
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    def _write_json(self, status: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?")[0] == "/health":
            self._write_json(200, {"status": "ok", "version": __version__})
        else:
            self._write_json(404, {"ok": False, "error": {
                "type": "NotFound", "message": f"unknown path {self.path}"}})

    def do_POST(self):
        path = self.path.split("?")[0]
        if path != "/analyze":
            self._write_json(404, {"ok": False, "error": {
                "type": "NotFound", "message": f"unknown path {self.path}"}})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY_BYTES:
            self._write_json(422, {"ok": False, "error": {
                "type": "BadRequest",
                "message": "missing or oversized Content-Length"}})
            return
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._write_json(422, {"ok": False, "error": {
                "type": "BadJson", "message": f"invalid JSON body: {exc}"}})
            return
        try:
            result = run_analysis_payload(payload)
        except TaintLangError as exc:
            self._write_json(400, {"ok": False, "error": exc.to_dict()})
        except ValueError as exc:
            self._write_json(422, {"ok": False, "error": {
                "type": "BadRequest", "message": str(exc)}})
        else:
            self._write_json(200, {"ok": True, "result": result})


def create_server(host: str = "127.0.0.1", port: int = 8080,
                  verbose: bool = False) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), _Handler)
    server.verbose = verbose
    return server


def serve(host: str = "127.0.0.1", port: int = 8080,
          verbose: bool = False) -> None:
    server = create_server(host, port, verbose)
    print(f"TaintLang service listening on http://{host}:{port}")
    print("POST /analyze  with {\"source\": ..., \"config\": {...}}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def serve_in_thread(host: str = "127.0.0.1", port: int = 0):
    """Start the server on a background thread (used by tests)."""
    server = create_server(host, port)
    actual_host, actual_port = server.server_address[:2]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, actual_host, actual_port
