"""JSON request/response entry point for the CCD solver.

Request schema (see examples/ for ready-made files):

    {
      "time_window": [0.0, 10.0],          // closed [t_min, t_max], optional, default [0, +inf]
      "robot":    {"position": [x, y], "velocity": [vx, vy], "radius": r},
      "obstacles": [
        {"id": "obs-1", "position": [x, y], "velocity": [vx, vy], "radius": r},
        ...
      ]
    }

Response:

    {
      "earliest": {"obstacle_id": ..., "status": ..., "time": ..., "distance_at_contact": ...} | null,
      "results": [
        {"obstacle_id": ..., "status": ..., "time": ..., "distance_at_contact": ...},
        ...
      ]
    }

All positions/velocities are 2-vectors, radii are positive numbers.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from ccd.models import CircleBody, ContactResult
from ccd.solver import earliest_contact


def _vec2(value: Any, field: str) -> np.ndarray:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{field} must be a 2-element array, got {value!r}")
    try:
        vec = np.asarray(value, dtype=float)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must contain numbers, got {value!r}") from exc
    if not np.all(np.isfinite(vec)):
        raise ValueError(f"{field} must contain finite numbers, got {value!r}")
    return vec


def _positive_float(value: Any, field: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{field} must be a number, got {value!r}")
    if not math.isfinite(value) or value <= 0.0:
        raise ValueError(f"{field} must be a positive finite number, got {value!r}")
    return float(value)


def _parse_body(spec: Any, field: str) -> CircleBody:
    if not isinstance(spec, dict):
        raise ValueError(f"{field} must be an object, got {spec!r}")
    for key in ("position", "velocity", "radius"):
        if key not in spec:
            raise ValueError(f"{field} is missing required key {key!r}")
    return CircleBody(
        position=_vec2(spec["position"], f"{field}.position"),
        velocity=_vec2(spec["velocity"], f"{field}.velocity"),
        radius=_positive_float(spec["radius"], f"{field}.radius"),
    )


def _result_to_dict(result: ContactResult, obstacle_id: str) -> dict[str, Any]:
    return {
        "obstacle_id": obstacle_id,
        "status": result.status.value,
        "time": result.time,
        "distance_at_contact": result.distance_at_contact,
    }


def solve_request(request: dict[str, Any]) -> dict[str, Any]:
    """Run one collision query per obstacle and return the JSON response."""
    if not isinstance(request, dict):
        raise ValueError("request must be a JSON object")
    if "robot" not in request:
        raise ValueError("request is missing required key 'robot'")
    if "obstacles" not in request:
        raise ValueError("request is missing required key 'obstacles'")

    robot = _parse_body(request["robot"], "robot")

    obstacles = request["obstacles"]
    if not isinstance(obstacles, list) or not obstacles:
        raise ValueError("'obstacles' must be a non-empty array")

    t_min, t_max = 0.0, math.inf
    if "time_window" in request:
        window = request["time_window"]
        if not isinstance(window, (list, tuple)) or len(window) != 2:
            raise ValueError("'time_window' must be a 2-element array [t_min, t_max]")
        t_min = _positive_float(window[0], "time_window[0]") if window[0] != 0 else 0.0
        if not isinstance(window[1], (int, float)) or isinstance(window[1], bool):
            raise ValueError("time_window[1] must be a number")
        t_max = float(window[1])
        if not (t_max >= t_min):
            raise ValueError(f"time_window end ({t_max}) must be >= start ({t_min})")

    results: list[dict[str, Any]] = []
    earliest: dict[str, Any] | None = None
    for index, obstacle_spec in enumerate(obstacles):
        obstacle_id = obstacle_spec.get("id", f"obstacle-{index}") if isinstance(obstacle_spec, dict) else f"obstacle-{index}"
        obstacle = _parse_body(obstacle_spec, f"obstacles[{index}]")
        result = earliest_contact(robot, obstacle, t_min, t_max)
        entry = _result_to_dict(result, obstacle_id)
        results.append(entry)
        if result.time is not None and (earliest is None or result.time < earliest["time"]):
            earliest = entry

    return {"earliest": earliest, "results": results}
