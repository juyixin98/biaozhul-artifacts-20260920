"""JSON HTTP service for the resource-release analyzer.

Pure standard library (``http.server``).  Exposes:

* ``POST /analyze``   body: ``{"source": "...", "loop_bound": 2,
  "max_paths": 512}`` (only ``source`` is required)
* ``GET  /health``    liveness probe
* ``GET  /version``   toolchain metadata

Analysis failures (lex/parse errors) are reported with HTTP 400 and a
structured body; they never crash the server.
"""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .errors import ResFlowError
from .engine import analyze, LANGUAGE_NAME, LANGUAGE_VERSION, \
    TOOLCHAIN_VERSION


class _Handler(BaseHTTPRequestHandler):
    server_version = f"{LANGUAGE_NAME}/{TOOLCHAIN_VERSION}"

    # -- helpers -----------------------------------------------------------

    def _send_json(self, status, payload):
        body = json.dumps(payload, indent=2, sort_keys=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # quiet, deterministic access log
        return

    # -- routes ------------------------------------------------------------

    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {"status": "ok"})
        elif self.path == "/version":
            self._send_json(200, {
                "language": LANGUAGE_NAME,
                "language_version": LANGUAGE_VERSION,
                "toolchain_version": TOOLCHAIN_VERSION,
            })
        else:
            self._send_json(404, {"error": {"message": "not found",
                                           "code": "not_found"}})

    def do_POST(self):
        if self.path != "/analyze":
            self._send_json(404, {"error": {"message": "not found",
                                           "code": "not_found"}})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                self._send_json(400, {"error": {
                    "code": "invalid_json",
                    "message": f"request body is not valid JSON: {exc}"}})
                return
            if not isinstance(payload, dict) or "source" not in payload:
                self._send_json(400, {"error": {
                    "code": "missing_source",
                    "message": "request must be a JSON object with a "
                               "'source' string field"}})
                return
            source = payload["source"]
            if not isinstance(source, str):
                self._send_json(400, {"error": {
                    "code": "invalid_source",
                    "message": "'source' must be a string"}})
                return
            loop_bound = _int_option(payload, "loop_bound", 2, minimum=0,
                                     maximum=10_000)
            max_paths = _int_option(payload, "max_paths", 512, minimum=1,
                                    maximum=100_000)
            if isinstance(loop_bound, tuple):
                self._send_json(400, {"error": loop_bound[1]})
                return
            if isinstance(max_paths, tuple):
                self._send_json(400, {"error": max_paths[1]})
                return
            try:
                report = analyze(source, loop_bound=loop_bound,
                                 max_paths=max_paths)
            except ResFlowError as exc:
                self._send_json(400, {"error": {
                    "code": "analysis_frontend_error",
                    "kind": type(exc).__name__,
                    **exc.to_dict()}})
                return
            self._send_json(200, report)
        except BrokenPipeError:
            return


def _int_option(payload, name, default, minimum, maximum):
    if name not in payload:
        return default
    value = payload[name]
    if isinstance(value, bool) or not isinstance(value, int):
        return (None, {"code": "invalid_option",
                       "message": f"{name!r} must be an integer"})
    if not minimum <= value <= maximum:
        return (None, {"code": "invalid_option",
                       "message": f"{name!r} must be within "
                                  f"[{minimum}, {maximum}]"})
    return value


def build_server(host="127.0.0.1", port=8080):
    return ThreadingHTTPServer((host, port), _Handler)


def serve(host="127.0.0.1", port=8080):
    httpd = build_server(host, port)
    print(f"{LANGUAGE_NAME} analysis service listening on "
          f"http://{host}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
