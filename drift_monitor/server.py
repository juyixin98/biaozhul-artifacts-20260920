"""Local JSON HTTP service for the drift monitor (stdlib only).

Endpoints
---------
GET  /health             liveness + fitted state
POST /fit                fit/reset the baseline monitor
POST /score              score a current window against the fitted baseline
GET  /baseline           inspect the frozen baseline bins
POST /demo/synthesize    fit + score a built-in synthetic scenario
POST /monitor/export     download the fitted monitor as JSON
POST /monitor/load       restore a monitor from exported JSON

Request bodies are JSON. Numeric feature columns are arrays of numbers or
``null`` (``null`` = missing, converted to NaN); non-finite tokens such as
``"NaN"`` are rejected deliberately. The service binds to localhost only
and keeps one monitor in process memory.

Run with::

    python -m drift_monitor.server --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

from .monitor import DriftMonitor
from .synthetic import make_dataset, SCENARIOS, FEATURE_NAMES
from .model import LogisticModel, roc_auc

MAX_COLUMN_LEN = 1_000_000
MAX_FEATURES = 1000


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# --------------------------------------------------------------------------- io
def _column_to_array(values, feature: str) -> np.ndarray:
    if not isinstance(values, list):
        raise ApiError(400, f"feature {feature!r} must be an array")
    if len(values) > MAX_COLUMN_LEN:
        raise ApiError(400, f"feature {feature!r} exceeds {MAX_COLUMN_LEN} rows")
    out = np.empty(len(values), dtype=np.float64)
    for i, v in enumerate(values):
        if v is None:
            out[i] = np.nan
        elif isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ApiError(
                400, f"feature {feature!r} index {i}: expected number or null, "
                     f"got {type(v).__name__}"
            )
        elif not math.isfinite(v):
            raise ApiError(
                400, f"feature {feature!r} index {i}: non-finite value; "
                     "send null for missing instead"
            )
        else:
            out[i] = float(v)
    return out


def _parse_table(payload, key: str) -> dict[str, np.ndarray]:
    if not isinstance(payload, dict):
        raise ApiError(400, "request body must be a JSON object")
    table = payload.get(key)
    if not isinstance(table, dict) or not table:
        raise ApiError(400, f"'{key}' must be a non-empty object of arrays")
    if len(table) > MAX_FEATURES:
        raise ApiError(400, f"too many features (max {MAX_FEATURES})")
    arrays: dict[str, np.ndarray] = {}
    lengths = {len(v) if isinstance(v, list) else None for v in table.values()}
    if len(lengths) != 1 or None in lengths:
        raise ApiError(400, f"all '{key}' columns must be arrays of equal length")
    for name, values in table.items():
        if not isinstance(name, str) or not name:
            raise ApiError(400, "feature names must be non-empty strings")
        arrays[name] = _column_to_array(values, name)
    return arrays


@dataclass
class _State:
    monitor: DriftMonitor | None = None


STATE = _State()


# --------------------------------------------------------------------- handlers
def _handle_fit(payload: dict) -> dict:
    data = _parse_table(payload, "data")
    n_bins = int(payload.get("n_bins", 10))
    strategy = str(payload.get("strategy", "quantile"))
    alpha = float(payload.get("alpha", 0.5))
    if not 1 <= n_bins <= 1000:
        raise ApiError(400, "n_bins must be in [1, 1000]")
    if strategy not in ("quantile", "uniform"):
        raise ApiError(400, "strategy must be 'quantile' or 'uniform'")
    if not 0.0 <= alpha <= 10.0:
        raise ApiError(400, "alpha must be in [0, 10]")
    monitor = DriftMonitor(n_bins=n_bins, strategy=strategy, alpha=alpha)
    try:
        monitor.fit(data)
    except ValueError as exc:
        raise ApiError(400, f"fit failed: {exc}") from exc
    STATE.monitor = monitor
    return {"fitted": True, "baseline": _baseline_summary(monitor)}


def _handle_score(payload: dict) -> dict:
    if STATE.monitor is None:
        raise ApiError(409, "monitor is not fitted; POST /fit first")
    data = _parse_table(payload, "data")
    try:
        result = STATE.monitor.score(data)
    except ValueError as exc:
        raise ApiError(400, str(exc)) from exc
    return result.to_dict()


def _baseline_summary(monitor: DriftMonitor) -> dict:
    return {
        "n_bins": monitor.n_bins,
        "strategy": monitor.strategy,
        "alpha": monitor.alpha,
        "features": [
            {
                "name": name,
                "baseline_total": monitor._baseline_total[name],
                "baseline_counts": [int(v) for v in monitor._baseline_counts[name]],
                "bucket_labels": __import__(
                    "drift_monitor.binning", fromlist=["bucket_labels"]
                ).bucket_labels(monitor._binners[name]),
                "edges": monitor._binners[name].to_dict()["edges"],
            }
            for name in monitor.feature_names
        ],
    }


def _handle_demo(payload: dict) -> dict:
    scenario = str(payload.get("scenario", "same"))
    if scenario not in SCENARIOS:
        raise ApiError(400, f"scenario must be one of {SCENARIOS}")
    seed = int(payload.get("seed", 42))
    n_baseline = int(payload.get("n_baseline", 5000))
    n_current = int(payload.get("n_current", 2000))
    ds = make_dataset(scenario, n_baseline=n_baseline, n_current=n_current,
                      seed=seed)
    monitor = DriftMonitor(n_bins=10, strategy="quantile", alpha=0.5)
    monitor.fit(ds.X_base)
    STATE.monitor = monitor
    drift = monitor.score(ds.X_cur).to_dict()

    # Model side: trained on baseline, evaluated on both windows.
    model = LogisticModel(FEATURE_NAMES).fit(ds.X_base, ds.y_base)
    auc_base = roc_auc(ds.y_base, model.predict_proba(ds.X_base))
    auc_cur = roc_auc(ds.y_cur, model.predict_proba(ds.X_cur))
    drift["model_auc"] = {
        "baseline": None if math.isnan(auc_base) else auc_base,
        "current": None if math.isnan(auc_cur) else auc_cur,
        "note": ("AUC is a performance proxy; covariate drift may or may not "
                 "move it - no threshold here proves concept drift."),
    }
    drift["scenario"] = scenario
    drift["seed"] = seed
    return drift


class Handler(BaseHTTPRequestHandler):
    server_version = "DriftMonitor/1.0"

    def _send(self, status: int, body: dict) -> None:
        raw = json.dumps(body, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            raise ApiError(400, "request body is required")
        if length > 50 * 1024 * 1024:
            raise ApiError(413, "request body too large (max 50 MiB)")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(400, f"invalid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ApiError(400, "request body must be a JSON object")
        return payload

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        try:
            if self.path == "/health":
                self._send(200, {
                    "status": "ok",
                    "fitted": STATE.monitor is not None,
                    "features": STATE.monitor.feature_names
                    if STATE.monitor else [],
                })
            elif self.path == "/baseline":
                if STATE.monitor is None:
                    raise ApiError(409, "monitor is not fitted; POST /fit first")
                self._send(200, {"baseline": _baseline_summary(STATE.monitor)})
            else:
                raise ApiError(404, "not found; see README for endpoints")
        except ApiError as exc:
            self._send(exc.status, {"error": exc.message})

    def do_POST(self) -> None:  # noqa: N802
        try:
            # /monitor/export takes no body; every other POST route does.
            if self.path == "/monitor/export":
                if STATE.monitor is None:
                    raise ApiError(409, "monitor is not fitted")
                self._send(200, {"monitor": STATE.monitor.to_dict()})
                return
            payload = self._read_json()
            if self.path == "/fit":
                self._send(200, _handle_fit(payload))
            elif self.path == "/score":
                self._send(200, _handle_score(payload))
            elif self.path == "/demo/synthesize":
                self._send(200, _handle_demo(payload))
            elif self.path == "/monitor/load":
                mon_payload = payload.get("monitor")
                if not isinstance(mon_payload, dict):
                    raise ApiError(400, "'monitor' object is required")
                try:
                    STATE.monitor = DriftMonitor.from_dict(mon_payload)
                except (KeyError, ValueError) as exc:
                    raise ApiError(400, f"invalid monitor payload: {exc}") from exc
                self._send(200, {"loaded": True,
                                 "features": STATE.monitor.feature_names})
            else:
                raise ApiError(404, "not found; see README for endpoints")
        except ApiError as exc:
            self._send(exc.status, {"error": exc.message})

    def log_message(self, fmt: str, *args) -> None:  # quieter, structured logs
        import sys
        sys.stderr.write(f"[server] {self.address_string()} - {fmt % args}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Drift monitor HTTP service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"drift monitor listening on http://{args.host}:{args.port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
