"""Local JSON-over-HTTP analysis service (Python stdlib only, no framework).

Endpoints
---------
POST /analyze
    Request body (JSON)::

        {
          "control":   [1.2, 3.4, null, ...],
          "treatment": [1.5, 2.9, ...],
          "confidence_level": 0.95,        # optional, default 0.95
          "missing_strategy": "drop"       # optional, "drop" | "impute_mean"
        }

    ``null`` encodes a missing observation. Response: the AnalysisResult
    fields as JSON, plus a ``validity_notice`` reminding the caller that
    the interval is only valid for a pre-registered fixed sample size.

GET /health
    Liveness probe, returns {"status": "ok"}.

Run:  python -m abtest.server [--host 127.0.0.1] [--port 8000]
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .stats import welch_mean_diff_ci

VALIDITY_NOTICE = (
    "Fixed-sample-size analysis only. This interval is NOT valid if you "
    "peeked at accumulating data and decided to stop based on the result; "
    "sequential monitoring inflates the false-positive rate. This service "
    "provides statistical computation, not business decision advice."
)

MAX_BODY_BYTES = 10 * 1024 * 1024  # 10 MiB request cap


def _parse_observations(value, name: str) -> list[float]:
    if not isinstance(value, list):
        raise ValueError(f"{name!r} must be a JSON array of numbers/null")
    out = []
    for i, item in enumerate(value):
        if item is None:
            out.append(float("nan"))
        elif isinstance(item, (int, float)) and not isinstance(item, bool):
            out.append(float(item))
        else:
            raise ValueError(f"{name}[{i}] must be a number or null")
    return out


def build_response(payload: dict) -> tuple[int, dict]:
    """Validate the request payload and compute the analysis.

    Returns (http_status, response_body). Pure function: directly testable
    without a running server.
    """
    if not isinstance(payload, dict):
        return 400, {"error": "request body must be a JSON object"}
    try:
        control = _parse_observations(payload.get("control"), "control")
        treatment = _parse_observations(payload.get("treatment"), "treatment")
        confidence = float(payload.get("confidence_level", 0.95))
        strategy = str(payload.get("missing_strategy", "drop"))
        result = welch_mean_diff_ci(
            control,
            treatment,
            confidence_level=confidence,
            missing_strategy=strategy,
        )
    except (ValueError, TypeError) as exc:
        return 400, {"error": str(exc)}
    body = result.to_dict()
    body["validity_notice"] = VALIDITY_NOTICE
    return 200, body


class AnalysisHandler(BaseHTTPRequestHandler):
    server_version = "abtest-fixed-sample/0.1"

    def _send_json(self, status: int, body: dict) -> None:
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
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 (stdlib naming)
        if self.path != "/analyze":
            self._send_json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self._send_json(411, {"error": "invalid Content-Length"})
            return
        if length <= 0 or length > MAX_BODY_BYTES:
            self._send_json(413, {"error": "request body missing or too large"})
            return
        try:
            payload = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            self._send_json(400, {"error": f"invalid JSON: {exc}"})
            return
        status, body = build_response(payload)
        self._send_json(status, body)

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        # Keep test output clean; uncomment for debugging.
        pass


def make_server(host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), AnalysisHandler)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fixed-sample A/B analysis service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    server = make_server(args.host, args.port)
    print(f"Serving on http://{args.host}:{args.port} (POST /analyze, GET /health)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
