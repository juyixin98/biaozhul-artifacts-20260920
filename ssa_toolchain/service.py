"""JSON service wrapping the SSA pipeline.

CLI
---
Read a request JSON from a file or stdin::

    python -m ssa_toolchain.service --request examples/requests/diamond.json
    cat req.json | python -m ssa_toolchain.service

HTTP (standard library only)::

    python -m ssa_toolchain.service --serve 8080
    curl -s localhost:8080/compile -d @examples/requests/diamond.json

Request fields
--------------
* ``source``   - mini source text, or
* ``file``     - path to a mini source file (server-side read; CLI only),
* ``entry``    - entry function name (default ``main``),
* ``inputs``   - integer arguments for the entry function,
* ``execute``  - whether to interpret and compare (default true when
  ``inputs`` given),
* ``include_ir`` - include raw/ssa/flat IR text (default true).

Response: ``{"ok": true, "result": {...}}`` or
``{"ok": false, "error": {...}}`` - always HTTP 200 for well-formed JSON.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .errors import MiniError
from .pipeline import error_to_json, pipeline_to_json, run_pipeline


def handle_request(req: dict, source_override: str | None = None,
                   file_override: str | None = None) -> dict:
    try:
        if source_override is not None:
            source = source_override
            file = file_override or "<input>"
        elif "source" in req:
            source = req["source"]
            file = req.get("file", "<input>")
        elif "file" in req:
            path = Path(req["file"])
            source = path.read_text(encoding="utf-8")
            file = str(path)
        else:
            return {"ok": False,
                    "error": {"kind": "BadRequest",
                              "message": "request needs 'source' or 'file'"}}

        entry = req.get("entry", "main")
        inputs = req.get("inputs")
        execute = req.get("execute", inputs is not None)
        include_ir = req.get("include_ir", True)

        res = run_pipeline(source, file=file, entry=entry,
                           inputs=list(inputs) if inputs is not None else None,
                           execute=bool(execute))
        body = pipeline_to_json(res, include_ir=bool(include_ir))
        return {"ok": True, "result": body}
    except MiniError as err:
        src = locals().get("source")
        return error_to_json(err, src)
    except RecursionError:
        return {"ok": False,
                "error": {"kind": "CompileError",
                          "message": "compiler recursion limit (input too deep)"}}


# --------------------------------------------------------------------- HTTP

def make_http_handler():
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, payload: dict):
            data = json.dumps(payload, indent=2).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path in ("/health", "/healthz"):
                self._send(200, {"ok": True, "service": "ssa-toolchain"})
                return
            self._send(404, {"ok": False,
                             "error": {"kind": "NotFound",
                                       "message": "POST /compile"}})

        def do_POST(self):
            if self.path != "/compile":
                self._send(404, {"ok": False,
                                 "error": {"kind": "NotFound",
                                           "message": "POST /compile"}})
                return
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b""
            try:
                req = json.loads(raw.decode("utf-8")) if raw else {}
                if not isinstance(req, dict):
                    raise ValueError("request body must be a JSON object")
            except (ValueError, UnicodeDecodeError) as e:
                self._send(400, {"ok": False,
                                 "error": {"kind": "BadRequest",
                                           "message": f"invalid JSON: {e}"}})
                return
            # Server mode never reads file paths from the request.
            self._send(200, handle_request(req))

        def log_message(self, fmt, *args):  # quiet by default
            sys.stderr.write("[ssa-service] " + fmt % args + "\n")

    return Handler


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="SSA construction/destruction service")
    ap.add_argument("--request", help="read request JSON from this file")
    ap.add_argument("--serve", metavar="PORT", type=int,
                    help="run an HTTP server on the given port")
    ap.add_argument("--source", help="compile this source text directly")
    ap.add_argument("--file", help="compile this mini source file directly")
    ap.add_argument("--input", dest="inputs", default="",
                    help="comma separated integer inputs for main()")
    ap.add_argument("--no-ir", action="store_true",
                    help="omit IR dumps from the response")
    args = ap.parse_args(argv)

    if args.serve is not None:
        from http.server import HTTPServer
        server = HTTPServer(("127.0.0.1", args.serve), make_http_handler())
        print(f"ssa-toolchain service listening on http://127.0.0.1:{args.serve}",
              file=sys.stderr)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        return 0

    if args.request:
        req = json.loads(Path(args.request).read_text(encoding="utf-8"))
    elif args.source is not None or args.file:
        inputs = [int(x) for x in args.inputs.split(",") if x.strip()]
        req = {"source": args.source} if args.source is not None else {"file": args.file}
        req["inputs"] = inputs
        if args.no_ir:
            req["include_ir"] = False
    elif not sys.stdin.isatty():
        req = json.loads(sys.stdin.read())
    else:
        ap.error("give --request, --source/--file, --serve, or pipe a request")

    resp = handle_request(req)
    print(json.dumps(resp, indent=2))
    return 0 if resp.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
