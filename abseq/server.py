"""Local JSON HTTP service for fixed-horizon A/B analysis (stdlib only).

Endpoints:
    GET  /health          -> {"ok": true, "data": {"status": "up"}}
    POST /v1/analyze      -> fixed-horizon A/B analysis
    POST /v1/coverage     -> Monte Carlo coverage simulation
    POST /v1/peeking      -> sequential-peeking type-I inflation simulation

All responses use the envelope {"ok": bool, "data": ..., "error": ...}.
Run with: python -m abseq.server [--host 127.0.0.1] [--port 8000]
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .analysis import analyze, analyze_groups
from .missing import STRATEGIES
from .simulation import coverage_simulation, peeking_type1_simulation

MAX_BODY_BYTES = 10 * 1024 * 1024


def _require(payload: dict, key: str):
    if key not in payload:
        raise ValueError(f"missing required field {key!r}")
    return payload[key]


def handle_analyze(payload: dict) -> dict:
    confidence = float(payload.get("confidence", 0.95))
    missing_strategy = str(payload.get("missing_strategy", "raise"))
    if missing_strategy not in STRATEGIES:
        raise ValueError(
            f"missing_strategy must be one of {list(STRATEGIES)}, "
            f"got {missing_strategy!r}"
        )
    if "groups" in payload or "observations" in payload:
        return analyze_groups(
            _require(payload, "groups"),
            _require(payload, "observations"),
            confidence=confidence,
            missing_strategy=missing_strategy,
        )
    return analyze(
        _require(payload, "control"),
        _require(payload, "treatment"),
        confidence=confidence,
        missing_strategy=missing_strategy,
    )


def handle_coverage(payload: dict) -> dict:
    return coverage_simulation(
        n_control=int(_require(payload, "n_control")),
        n_treatment=int(_require(payload, "n_treatment")),
        effect=float(payload.get("effect", 0.0)),
        reps=int(payload.get("reps", 2000)),
        confidence=float(payload.get("confidence", 0.95)),
        missing_strategy=str(payload.get("missing_strategy", "drop")),
        missing_rate=float(payload.get("missing_rate", 0.0)),
        seed=int(payload.get("seed", 0)),
    )


def handle_peeking(payload: dict) -> dict:
    return peeking_type1_simulation(
        n_per_group=int(_require(payload, "n_per_group")),
        looks=int(payload.get("looks", 5)),
        reps=int(payload.get("reps", 2000)),
        alpha=float(payload.get("alpha", 0.05)),
        seed=int(payload.get("seed", 0)),
    )


POST_ROUTES = {
    "/v1/analyze": handle_analyze,
    "/v1/coverage": handle_coverage,
    "/v1/peeking": handle_peeking,
}


class Handler(BaseHTTPRequestHandler):
    server_version = "abseq/0.1"

    def _send_json(self, status: int, body: dict) -> None:
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        if self.path == "/health":
            self._send_json(200, {"ok": True, "data": {"status": "up"}, "error": None})
        else:
            self._send_json(404, {"ok": False, "data": None, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 (stdlib naming)
        handler = POST_ROUTES.get(self.path)
        if handler is None:
            self._send_json(404, {"ok": False, "data": None, "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send_json(400, {"ok": False, "data": None, "error": "bad Content-Length"})
            return
        if length <= 0 or length > MAX_BODY_BYTES:
            self._send_json(400, {"ok": False, "data": None, "error": "invalid body size"})
            return
        try:
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            data = handler(payload)
        except (ValueError, TypeError, KeyError) as exc:
            self._send_json(400, {"ok": False, "data": None, "error": str(exc)})
            return
        self._send_json(200, {"ok": True, "data": data, "error": None})

    def log_message(self, format: str, *args) -> None:
        # Keep the service quiet by default; errors are returned to clients.
        return


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="abseq local analysis service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"abseq service listening on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
