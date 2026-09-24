"""Deterministic experiment computation.

This is a *real*, reproducible numeric pipeline (not a stub): it parses the
synthetic bag file, folds in parameters, calibration matrix and the run seed,
and produces both an in-memory results summary and an output artifact file.

Determinism contract
--------------------
Given identical (bag_bytes, params, calibration_matrix, seed) the pipeline
produces byte-identical output artifacts. Geometry (centroid/variance) is a pure
function of bag + params + calibration and is independent of the seed; the seed
only perturbs the final scalar ``score`` through a SHA-256 keyed mixing step.
The PRNG is a seeded SHA-256 based generator, so results never depend on
``random``/OS entropy. The output file is written atomically (temp file +
``os.replace``).

Synthetic bag format
--------------------
A tiny text format, one record per line::

    # comments allowed
    FLOAT,FLOAT,FLOAT[,WEIGHT]

Weights default to 1.0. Blank lines and comments are tolerated; any other line
raises ``BagFormatError`` so malformed input is reported honestly.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .crypto import canonical_json


class BagFormatError(ValueError):
    pass


@dataclass(frozen=True)
class Point:
    x: float
    y: float
    z: float
    w: float


def parse_bag(data: bytes) -> list[Point]:
    points: list[Point] = []
    for lineno, raw in enumerate(data.decode("utf-8", errors="strict").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(",")
        if len(parts) not in (3, 4):
            raise BagFormatError(
                f"line {lineno}: expected 3 or 4 comma-separated values, got {len(parts)}"
            )
        try:
            vals = [float(p.strip()) for p in parts]
        except ValueError as exc:
            raise BagFormatError(f"line {lineno}: non-numeric value ({exc})") from exc
        x, y, z = vals[:3]
        w = vals[3] if len(vals) == 4 else 1.0
        if not all(math.isfinite(v) for v in vals):
            raise BagFormatError(f"line {lineno}: values must be finite")
        points.append(Point(x, y, z, w))
    if not points:
        raise BagFormatError("bag contains no data records")
    return points


def matvec(matrix: list[list[float]], vec: tuple[float, float, float, float]) -> tuple[float, float, float]:
    out = []
    for row in matrix[:3]:
        out.append(sum(a * b for a, b in zip(row, vec)))
    return out[0], out[1], out[2]


def run_pipeline(
    bag_bytes: bytes,
    params: dict[str, Any],
    calibration_matrix: list[list[float]],
    seed: int,
) -> dict[str, Any]:
    """Execute the experiment. Returns a JSON-serializable results summary.

    Geometry depends only on the data + params + calibration. The seed enters
    solely through the final deterministic scalar score, which is why two runs
    with different seeds share geometry but get different scores (and vice
    versa: identical inputs + seed give byte-identical results).
    """
    points = parse_bag(bag_bytes)

    gain = float(params.get("gain", 1.0))
    offset = float(params.get("offset", 0.0))

    transformed: list[tuple[float, float, float, float]] = []
    sx = sy = sz = sw = 0.0
    for p in points:
        tx, ty, tz = matvec(calibration_matrix, (p.x, p.y, p.z, 1.0))
        vx = tx * gain + offset
        vy = ty * gain
        vz = tz * gain
        transformed.append((vx, vy, vz, p.w))
        sx += vx * p.w
        sy += vy * p.w
        sz += vz * p.w
        sw += p.w

    centroid = (sx / sw, sy / sw, sz / sw)
    variance = 0.0
    for vx, vy, vz, w in transformed:
        variance += w * ((vx - centroid[0]) ** 2 + (vy - centroid[1]) ** 2 + (vz - centroid[2]) ** 2)
    variance /= sw

    # Geometry digest: seed-independent.
    geom_h = hashlib.sha256()
    geom_h.update(canonical_json(params))
    geom_h.update(canonical_json(calibration_matrix))
    geom_h.update(f"{len(points)}:".encode())
    for t in transformed:
        geom_h.update(f"{t[0]:.12g},{t[1]:.12g},{t[2]:.12g},{t[3]:.12g}\n".encode())
    geometry_digest = geom_h.hexdigest()

    # Seeded score: keyed mix of geometry with the run seed. Deterministic and
    # OS-entropy free. Different seed -> different score; same seed -> same.
    score_h = hashlib.sha256()
    score_h.update(b"robot-experiment-score/v1:")
    score_h.update(geometry_digest.encode())
    score_h.update(seed.to_bytes(16, "big"))
    score = int.from_bytes(score_h.digest()[:8], "big") / 2**64

    return {
        "point_count": len(points),
        "centroid": [centroid[0], centroid[1], centroid[2]],
        "weighted_variance": variance,
        "geometry_digest": geometry_digest,
        "score": score,
        "seed": seed,
    }


def render_artifact(
    *,
    snapshot_id: str,
    seed: int,
    bag_digest: str,
    params_digest: str,
    calibration_digest: str,
    results: dict[str, Any],
    input_digest: str,
) -> bytes:
    """Render the JSON output artifact (deterministic byte layout).

    Attempt identity is intentionally NOT embedded: identical content re-runs
    must produce byte-identical artifacts, and the attempt is already identified
    by its evidence directory (``evidence/jobs/<job_id>/``) and the database.
    """
    doc = {
        "artifact_type": "robot_experiment_result",
        "snapshot_id": snapshot_id,
        "input": {
            "bag_digest": bag_digest,
            "params_digest": params_digest,
            "calibration_digest": calibration_digest,
            "seed": seed,
            "input_digest": input_digest,
        },
        "results": results,
    }
    return canonical_json(doc) + b"\n"


def write_artifact_atomic(directory: Path, filename: str, payload: bytes) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    final = directory / filename
    tmp = final.with_name(f".{filename}.tmp-{os.getpid()}")
    with open(tmp, "wb") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, final)
    return final
