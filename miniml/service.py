"""JSON HTTP service for MiniML (Python standard library only).

Endpoints
---------
``POST /infer`` — parse + type-infer a program.
    request : {"source": str, "value_restriction"?: bool, "trace"?: bool}
    response 200 : {"ok": true, "bindings": [...], "final_type"?: str,
                    "trace"?: [...]}
    response 400 : {"ok": false, "error": {"code", "phase", "message",
                     "location", "rendered"}}

``POST /eval`` — infer first, then evaluate when typing succeeds.
    same request body; successful responses additionally carry
    ``eval_output`` and ``final_value``.

``GET  /health`` — liveness probe.

Run with ``python -m miniml.service [--host 127.0.0.1] [--port 8000]``.
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .pipeline import CompileFailure, compile_source, evaluate, serialize_result
from .eval import _fmt  # internal formatter used for the value field


def _error_payload(f: CompileFailure) -> dict:
    return {
        "ok": False,
        "error": {
            "code": f.code,
            "phase": f.phase,
            "message": f.message,
            "location": {
                "start": {"line": f.span.start.line, "column": f.span.start.col},
                "end": {"line": f.span.end.line, "column": f.span.end.col},
            },
            "rendered": f.rendered,
        },
    }


def handle_infer(body: dict) -> tuple[int, dict]:
    src = body.get("source")
    if not isinstance(src, str):
        return 400, {
            "ok": False,
            "error": {
                "code": "E000",
                "phase": "request",
                "message": "missing or non-string field 'source'",
            },
        }
    vr = body.get("value_restriction", True)
    trace = body.get("trace", True)
    try:
        _, result = compile_source(src, value_restriction=bool(vr), trace=bool(trace))
    except CompileFailure as f:
        return 400, _error_payload(f)
    return 200, {"ok": True, **serialize_result(result)}


def handle_eval(body: dict) -> tuple[int, dict]:
    src = body.get("source")
    if not isinstance(src, str):
        return 400, {
            "ok": False,
            "error": {
                "code": "E000",
                "phase": "request",
                "message": "missing or non-string field 'source'",
            },
        }
    vr = body.get("value_restriction", True)
    trace = body.get("trace", True)
    try:
        program, result = compile_source(src, value_restriction=bool(vr), trace=bool(trace))
        ev = evaluate(program)
    except CompileFailure as f:
        return 400, _error_payload(f)
    payload = serialize_result(result, evaluate_output=ev.output)
    if ev.value is not None:
        payload["final_value"] = _fmt(ev.value)
    payload["ok"] = True
    return 200, payload


ROUTES = {
    "/infer": handle_infer,
    "/eval": handle_eval,
}


class _Handler(BaseHTTPRequestHandler):
    server_version = "MiniML/1.0"

    def _send_json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 (stdlib API)
        if self.path.split("?")[0] == "/health":
            self._send_json(200, {"ok": True, "service": "miniml"})
            return
        self._send_json(404, {"ok": False, "error": {"message": "not found"}})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        handler = ROUTES.get(path)
        if handler is None:
            self._send_json(404, {"ok": False, "error": {"message": "not found"}})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
            if not isinstance(body, dict):
                raise ValueError("body must be a JSON object")
        except (ValueError, UnicodeDecodeError) as exc:
            self._send_json(
                400,
                {"ok": False, "error": {"code": "E000", "phase": "request",
                                        "message": f"invalid JSON: {exc}"}},
            )
            return
        status, payload = handler(body)
        self._send_json(status, payload)

    def log_message(self, fmt: str, *args) -> None:  # quieter tests
        if self.server.log_requests:  # type: ignore[attr-defined]
            super().log_message(fmt, *args)


def make_server(host: str = "127.0.0.1", port: int = 8000,
                log_requests: bool = True) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), _Handler)
    httpd.log_requests = log_requests  # type: ignore[attr-defined]
    return httpd


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="MiniML JSON type-inference service")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--quiet", action="store_true", help="suppress request logs")
    args = ap.parse_args(argv)
    httpd = make_server(args.host, args.port, log_requests=not args.quiet)
    print(f"MiniML service listening on http://{args.host}:{args.port}")
    print("endpoints: POST /infer, POST /eval, GET /health")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
