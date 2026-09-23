"""JSON HTTP service for the interval analyzer (Python standard library only).

POST /analyze
    request:  {"source": "<IntervalLang source>", "indent": optional int}
    200:      full analysis result (see README)
    400:      {"status":"error", ...} for lex/parse/analysis errors
POST /run
    request:  {"source": "...", "inputs": [int, ...]}
    200:      concrete execution result (runtime_error present + code field)
GET  /healthz -> {"status":"ok"}

Start:  python -m interval_ai.service [--host 127.0.0.1] [--port 8080]
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .concrete import run_source
from .errors import IvlError
from .pipeline import analyze_source, error_to_dict


def _analyze_payload(body: bytes) -> tuple[int, dict]:
    try:
        req = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        return 400, {"status": "error", "error": "InvalidJSON",
                     "message": str(e)}
    if not isinstance(req, dict) or not isinstance(req.get("source"), str):
        return 400, {"status": "error", "error": "InvalidRequest",
                     "message": "body must be JSON with a string 'source'"}
    try:
        res = analyze_source(req["source"])
    except IvlError as e:
        return 400, error_to_dict(e)
    return 200, res.to_dict()


def _run_payload(body: bytes) -> tuple[int, dict]:
    try:
        req = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        return 400, {"status": "error", "error": "InvalidJSON",
                     "message": str(e)}
    if not isinstance(req, dict) or not isinstance(req.get("source"), str):
        return 400, {"status": "error", "error": "InvalidRequest",
                     "message": "body must be JSON with a string 'source'"}
    inputs = req.get("inputs", [])
    if not isinstance(inputs, list) or not all(
            isinstance(x, int) and not isinstance(x, bool) for x in inputs):
        return 400, {"status": "error", "error": "InvalidRequest",
                     "message": "'inputs' must be a list of integers"}
    try:
        cr = run_source(req["source"], inputs)
    except IvlError as e:
        return 400, error_to_dict(e)
    out = {
        "status": "ok",
        "consumed_inputs": cr.consumed_inputs,
        "variables": cr.env,
        "arrays": cr.arrays,
    }
    if cr.error is not None:
        out["runtime_error"] = {
            "kind": cr.error.kind,
            "message": str(cr.error),
            "location": (cr.error.span.to_dict()
                         if cr.error.span is not None else None),
        }
    return 200, out


class _Handler(BaseHTTPRequestHandler):
    server_version = "IntervalAI/1.0"

    def _send(self, code: int, payload: dict) -> None:
        data = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._send(200, {"status": "ok", "service": "interval-ai"})
        else:
            self._send(404, {"status": "error", "error": "NotFound",
                             "message": self.path})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length)
        if self.path == "/analyze":
            code, payload = _analyze_payload(body)
        elif self.path == "/run":
            code, payload = _run_payload(body)
        else:
            code, payload = 404, {"status": "error", "error": "NotFound",
                                  "message": self.path}
        self._send(code, payload)

    def log_message(self, fmt, *args) -> None:  # quieter logs
        return


def serve(host: str = "127.0.0.1", port: int = 8080) -> None:
    httpd = ThreadingHTTPServer((host, port), _Handler)
    print(f"IntervalAI JSON service on http://{host}:{port} "
          f"(POST /analyze, POST /run, GET /healthz)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="interval_ai.service")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args(argv)
    serve(args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
