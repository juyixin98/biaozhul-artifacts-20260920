"""Service layer: request -> optimizer -> response, plus run-record persistence."""
from __future__ import annotations

import json
import math
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .geometry import Rect
from .integrity import canonical_json_bytes, sha256_of
from .schemas import SmoothRequest
from .smoother import SmoothConfig, smooth_path

RUN_DIR_ENV = "TRAJ_SMOOTH_RUN_DIR"


def _rects(obstacles) -> list[Rect]:
    return [Rect.from_center_wh(o.cx, o.cy, o.width, o.height) for o in obstacles]


def _json_default(obj):
    if isinstance(obj, (np.floating, np.integer)):
        return obj.item()
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    raise TypeError(f"not JSON serializable: {type(obj)!r}")


def _sanitize(obj):
    """Replace NaN/+Inf/-Inf with None so the payload is strict JSON and
    hashable with allow_nan=False (real cryptographic digest)."""
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, (np.floating,)):
        v = float(obj)
        return v if math.isfinite(v) else None
    return obj


def run_smoothing(request: SmoothRequest, raw_request: dict | None = None) -> tuple[dict, dict]:
    """Execute the solver. Returns (response_dict, run_record_dict).

    ``raw_request`` is the exact JSON object received on the wire; its
    canonical encoding is what ``request_sha256`` commits to, so a caller can
    verify the digest against the bytes they sent (defaults filled in by the
    schema are not part of it).
    """
    raw_points = np.array(request.path, dtype=float)
    rects = _rects(request.obstacles)
    p = request.params
    cfg = SmoothConfig(
        max_iterations=p.max_iterations,
        timeout_seconds=p.timeout_seconds,
        curvature_cap=p.curvature_cap,
        corridor=p.corridor,
        clearance=p.clearance,
        min_edge_length=p.min_edge_length,
        deviation_weight=p.deviation_weight,
        bend_weight=p.bend_weight,
        jerk_weight=p.jerk_weight,
        optimize_midpoint_clearance=p.optimize_midpoint_clearance,
    )
    res = smooth_path(raw_points, rects, cfg)

    points = [(float(q[0]), float(q[1])) for q in res.points]
    # Strict-JSON conversion (NaN/Inf -> null) before hashing and response.
    residuals_out = _sanitize(json.loads(json.dumps(res.residuals, default=_json_default)))
    verification_out = (
        _sanitize(json.loads(json.dumps(res.verification, default=_json_default)))
        if res.verification else None
    )

    components = {k: float(v) for k, v in res.objective_components.items()}

    response_body = {
        "status": res.status,
        "success": res.converged and res.status == "optimal",
        "message": res.message,
        "points": points,
        "point_count": len(points),
        "fixed_start": points[0],
        "fixed_goal": points[-1],
        "iterations": res.iterations,
        "max_iterations": p.max_iterations,
        "objective": float(res.objective),
        "objective_components": components,
        "residuals": residuals_out,
        "verification": verification_out,
        "elapsed_seconds": float(res.elapsed_seconds),
        "n_variables": res.n_variables,
        "n_control_points": res.n_control_points,
        "collapsed_duplicates": res.collapsed_duplicates,
        "parameters": _sanitize(json.loads(json.dumps(res.parameters, default=_json_default))),
        "request_id": request.request_id,
        "request_sha256": sha256_of(_sanitize(raw_request)
                                    if raw_request is not None
                                    else _sanitize(_request_hash_obj(request))),
    }
    response_body["response_sha256"] = sha256_of(response_body)

    run_record = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "run_id": uuid.uuid4().hex,
        "request": raw_request if raw_request is not None else json.loads(request.model_dump_json()),
        "response": response_body,
    }
    return response_body, run_record


def _request_hash_obj(request: SmoothRequest):
    return json.loads(request.model_dump_json())


def save_run_record(record: dict) -> str | None:
    """Persist a run record (request + response + digests) to disk.

    Directory defaults to ./runs; override with TRAJ_SMOOTH_RUN_DIR.  Set it
    to an empty string to disable persistence.
    """
    run_dir = os.environ.get(RUN_DIR_ENV, str(Path.cwd() / "runs"))
    if not run_dir:
        return None
    path = Path(run_dir)
    path.mkdir(parents=True, exist_ok=True)
    ts = record["timestamp_utc"].replace(":", "").replace("-", "").split(".")[0]
    fname = f"{ts}_{record['run_id'][:12]}.json"
    (path / fname).write_bytes(canonical_json_bytes(record))
    return str(path / fname)
