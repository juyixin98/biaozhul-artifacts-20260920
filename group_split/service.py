"""Local HTTP JSON service for group-constrained stratified splitting.

Stdlib-only (``http.server``); NumPy does the math. No external models or
data are involved.

Endpoints:
    GET  /health  -> {"status": "ok"}
    POST /split   -> see README for the request/response schema.

Run:
    python -m group_split.service --host 127.0.0.1 --port 8080
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import numpy as np

from group_split.splitter import DEFAULT_TOLERANCE, split_groups


def handle_split(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate the request payload and run the split."""
    if not isinstance(payload, dict):
        raise ValueError("request body must be a JSON object")
    missing = {"groups", "labels", "splits"} - payload.keys()
    if missing:
        raise ValueError(f"missing required field(s): {sorted(missing)}")

    ratios = payload["splits"]
    if not isinstance(ratios, dict):
        raise ValueError("'splits' must be an object mapping name -> ratio")

    result = split_groups(
        groups=np.asarray(payload["groups"]),
        labels=np.asarray(payload["labels"]),
        ratios=ratios,
        seed=int(payload.get("seed", 0)),
        tolerance=float(payload.get("tolerance", DEFAULT_TOLERANCE)),
    )

    response: dict[str, Any] = {
        "assignment": {str(g): s for g, s in result.assignment.items()},
        "split_sizes": {
            name: int(idx.size) for name, idx in result.split_indices.items()
        },
        "report": result.report,
    }
    if payload.get("include_sample_splits"):
        sample_splits = np.empty(len(payload["groups"]), dtype=object)
        for name, idx in result.split_indices.items():
            sample_splits[idx] = name
        response["sample_splits"] = sample_splits.tolist()
    return response


class SplitRequestHandler(BaseHTTPRequestHandler):
    server_version = "GroupSplit/0.1"

    def _send_json(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        if self.path == "/health":
            self._send_json(200, {"status": "ok"})
        else:
            self._send_json(404, {"error": f"unknown path: {self.path}"})

    def do_POST(self) -> None:  # noqa: N802 (stdlib naming)
        if self.path != "/split":
            self._send_json(404, {"error": f"unknown path: {self.path}"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json(400, {"error": f"invalid JSON body: {exc}"})
            return
        try:
            self._send_json(200, handle_split(payload))
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})

    def log_message(self, format: str, *args: Any) -> None:
        # Keep test output clean; uncomment for debugging.
        pass


def make_server(host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), SplitRequestHandler)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    server = make_server(args.host, args.port)
    print(f"serving on http://{args.host}:{args.port} (POST /split, GET /health)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
