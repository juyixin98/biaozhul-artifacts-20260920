"""JSON HTTP service for resource-release analysis (standard library only).

POST /analyze
    Body: {"source": "...", "filename": "example.rf" (optional),
           "loop_bound": 2 (optional), "max_steps": 10000 (optional)}
    Reply 200: the full analysis result (see README).
    Reply 400: {"error": {"code", "message", "span"}} for lex/parse/semantic
               problems or malformed requests.
GET /health -> {"status": "ok", "language": "resflow/1"}
Other routes return 404; non-POST on /analyze returns 405.
"""
from __future__ import annotations

import json
from argparse import ArgumentParser, Namespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

from .analyzer import analyze_source
from .errors import ResflowError


class AnalysisHandler(BaseHTTPRequestHandler):
    server_version = "resflow-json/1.0"

    # Quiet the default noisy logging; one concise line per request is enough.
    def log_message(self, fmt: str, *args: Any) -> None:
        if self.server.log_requests:  # type: ignore[attr-defined]
            super().log_message(fmt, *args)

    def _write_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        if self.path.split("?")[0] == "/health":
            self._write_json(200, {"status": "ok", "language": "resflow/1"})
            return
        self._write_json(404, {"error": {"code": "E-HTTP", "message": "not found", "span": None}})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?")[0] != "/analyze":
            self._write_json(404, {"error": {"code": "E-HTTP", "message": "not found", "span": None}})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._bad_request("invalid Content-Length header", None)
            return
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._bad_request(f"request body must be UTF-8 JSON: {exc}", None)
            return
        if not isinstance(payload, dict):
            self._bad_request("request body must be a JSON object", None)
            return

        source = payload.get("source")
        if not isinstance(source, str):
            self._bad_request("field 'source' is required and must be a string", None)
            return
        filename = payload.get("filename", "<http>")
        if not isinstance(filename, str):
            self._bad_request("field 'filename' must be a string", None)
            return
        loop_bound = self._int_option(payload, "loop_bound", 2)
        max_steps = self._int_option(payload, "max_steps", 10000)
        if isinstance(loop_bound, str):
            self._bad_request(loop_bound, None)
            return
        if isinstance(max_steps, str):
            self._bad_request(max_steps, None)
            return

        try:
            result = analyze_source(source, filename=filename, loop_bound=loop_bound, max_steps=max_steps)
        except ResflowError as exc:
            self._write_json(400, {"error": exc.to_dict()})
            return
        self._write_json(200, result)

    def _int_option(self, payload: dict, name: str, default: int) -> Any:
        if name not in payload:
            return default
        value = payload[name]
        if isinstance(value, bool) or not isinstance(value, int):
            return f"field {name!r} must be an integer"
        return value

    def _bad_request(self, message: str, span: Optional[dict]) -> None:
        self._write_json(
            400,
            {"error": {"code": "E-REQUEST", "message": message, "span": span}},
        )


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(description="Run the resflow JSON analysis service.")
    parser.add_argument("--host", default="127.0.0.1", help="bind host (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080, help="bind port (default 8080)")
    parser.add_argument("--quiet", action="store_true", help="suppress per-request log lines")
    return parser


def run(argv: Optional[list] = None) -> int:
    args: Namespace = build_parser().parse_args(argv)
    server = ThreadingHTTPServer((args.host, args.port), AnalysisHandler)
    server.log_requests = not args.quiet  # type: ignore[attr-defined]
    print(f"resflow JSON service listening on http://{args.host}:{args.port}")
    print("POST /analyze with {\"source\": \"...\"}; GET /health")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
