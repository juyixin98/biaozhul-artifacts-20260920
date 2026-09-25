"""Backend HTTP JSON service using only the Python standard library.

Endpoints
---------
GET  /health   liveness + version info
POST /split    split a caller-supplied dataset
POST /demo     run the offline synthetic pipeline
GET  /         endpoint listing (JSON)

Request / response bodies are JSON. No third-party web framework, no network
access to anything external.
"""
from __future__ import annotations

import argparse
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .models import SplitResult
from .pipeline import run_synthetic_demo
from .splitter import split_dataset

LOG = logging.getLogger("grouped_splitter.service")
MAX_BODY_BYTES = 8 * 1024 * 1024


# --------------------------------------------------------------------------
# Serialization
# --------------------------------------------------------------------------

def result_to_dict(result: SplitResult, *, include_assignments: bool = True) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "split_names": result.split_names,
        "ratios": result.ratios,
        "seed": result.seed,
        "n_samples": result.n_samples,
        "n_groups": result.n_groups,
        "split_sizes": result.split_sizes,
        "group_assignments": result.group_assignments,
        "class_deviations": [
            {
                "label": cd.label,
                "global_count": cd.global_count,
                "global_proportion": cd.global_proportion,
                "split_counts": cd.split_counts,
                "split_proportions": cd.split_proportions,
                "max_abs_deviation": cd.max_abs_deviation,
            }
            for cd in result.class_deviations
        ],
        "diagnostics": {
            "tolerance": result.diagnostics.tolerance,
            "stratified_within_tolerance":
                result.diagnostics.stratified_within_tolerance,
            "counts_within_tolerance": result.diagnostics.counts_within_tolerance,
            "max_class_proportion_deviation":
                result.diagnostics.max_class_proportion_deviation,
            "max_split_count_deviation":
                result.diagnostics.max_split_count_deviation,
            "reasons": result.diagnostics.reasons,
            "rare_classes": result.diagnostics.rare_classes,
            "oversized_groups": result.diagnostics.oversized_groups,
        },
        "groups": [
            {
                "group_id": g.group_id,
                "label": g.label,
                "class_counts": g.class_counts,
                "size": g.size,
                "split": g.split,
            }
            for g in result.group_reports
        ],
    }
    if include_assignments:
        payload["assignments"] = result.assignments
    return payload


# --------------------------------------------------------------------------
# Request handlers
# --------------------------------------------------------------------------

def handle_split(body: dict[str, Any]) -> dict[str, Any]:
    groups = body.get("groups")
    labels = body.get("labels")
    if not isinstance(groups, list) or not isinstance(labels, list):
        raise _BadRequest("fields 'groups' and 'labels' must be JSON arrays")
    if not groups:
        raise _BadRequest("'groups' must contain at least one element")

    ratios = body.get("ratios", (0.7, 0.15, 0.15))
    names = body.get("split_names")
    result = split_dataset(
        groups=groups,
        labels=labels,
        ratios=ratios if isinstance(ratios, dict) else tuple(ratios),
        split_names=names,
        seed=int(body.get("seed", 42)),
        tolerance=float(body.get("tolerance", 0.05)),
    )
    include = bool(body.get("include_assignments", True))
    return result_to_dict(result, include_assignments=include)


def handle_demo(body: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "n_samples", "n_features", "n_classes", "n_groups",
        "seed", "tolerance",
    }
    kwargs: dict[str, Any] = {}
    for key in allowed:
        if key in body:
            kwargs[key] = int(body[key]) if key != "tolerance" else float(body[key])
    if "ratios" in body:
        ratios = body["ratios"]
        kwargs["ratios"] = ratios if isinstance(ratios, dict) else tuple(ratios)
    return run_synthetic_demo(**kwargs)


class _BadRequest(Exception):
    """Raised for malformed client requests -> HTTP 400."""


class SplitHTTPRequestHandler(BaseHTTPRequestHandler):
    server_version = "GroupedSplitter/1.0"

    def _write_json(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            raise _BadRequest("request body must be a JSON object")
        if length > MAX_BODY_BYTES:
            raise _BadRequest(f"request body too large (limit {MAX_BODY_BYTES} bytes)")
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _BadRequest(f"invalid JSON body: {exc}") from exc
        if not isinstance(body, dict):
            raise _BadRequest("request body must be a JSON object")
        return body

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        if self.path == "/health":
            self._write_json(200, {"status": "ok", "service": "grouped-splitter"})
        elif self.path in ("/", "/index"):
            self._write_json(200, {
                "service": "grouped-splitter",
                "endpoints": {
                    "GET /health": "liveness probe",
                    "POST /split": "split {groups, labels, ratios?, seed?, tolerance?}",
                    "POST /demo": "run reproducible synthetic pipeline",
                },
            })
        else:
            self._write_json(404, {"error": f"unknown path {self.path!r}"})

    def do_POST(self) -> None:  # noqa: N802
        try:
            body = self._read_json()
            if self.path == "/split":
                payload = handle_split(body)
            elif self.path == "/demo":
                payload = handle_demo(body)
            else:
                self._write_json(404, {"error": f"unknown path {self.path!r}"})
                return
            self._write_json(200, payload)
        except _BadRequest as exc:
            self._write_json(400, {"error": str(exc)})
        except ValueError as exc:
            # Validation errors from the core API are client errors.
            self._write_json(422, {"error": str(exc)})
        except Exception:  # noqa: BLE001 - never leak a traceback as a socket hangup
            LOG.exception("unhandled error processing %s", self.path)
            self._write_json(500, {"error": "internal server error"})

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.info("%s - %s", self.address_string(), fmt % args)


def build_server(host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), SplitHTTPRequestHandler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Group-aware stratified split service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    server = build_server(args.host, args.port)
    LOG.info("listening on http://%s:%d", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("shutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
