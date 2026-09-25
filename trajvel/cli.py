"""JSON command-line entry point.

Usage:
    python -m trajvel.cli request.json [response.json]
    cat request.json | python -m trajvel.cli            # stdin -> stdout

The request is a JSON object; the response is a JSON object with the
parameterization result. Validation errors are reported as a JSON object
on stderr with exit code 2.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import numpy as np

from .parameterize import CornerStrategy, parameterize
from .sampling import sample_trajectory

_REQUIRED_CONSTRAINTS = (
    "max_velocity",
    "max_acceleration",
    "max_lateral_acceleration",
)


class RequestError(ValueError):
    """Raised for malformed requests; reported as a JSON error."""


def _require(mapping: dict, key: str) -> Any:
    if key not in mapping:
        raise RequestError(f"missing required field: {key!r}")
    return mapping[key]


def _positive_float(mapping: dict, key: str) -> float:
    value = mapping[key]
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise RequestError(f"field {key!r} must be a number, got {value!r}")
    return float(value)


def parse_request(data: Any) -> dict:
    """Validate a decoded JSON request and normalize it into kwargs."""
    if not isinstance(data, dict):
        raise RequestError("request must be a JSON object")
    waypoints = _require(data, "waypoints")
    constraints = _require(data, "constraints")
    if not isinstance(constraints, dict):
        raise RequestError("field 'constraints' must be an object")
    for key in _REQUIRED_CONSTRAINTS:
        _require(constraints, key)

    strategy = data.get("corner_strategy", CornerStrategy.CURVATURE.value)
    try:
        CornerStrategy(strategy)
    except ValueError as exc:
        raise RequestError(
            f"field 'corner_strategy' must be one of "
            f"{[s.value for s in CornerStrategy]}, got {strategy!r}"
        ) from exc

    sample_dt = data.get("sample_dt")
    if sample_dt is not None:
        if not isinstance(sample_dt, (int, float)) or isinstance(sample_dt, bool) or sample_dt <= 0:
            raise RequestError(f"field 'sample_dt' must be a positive number, got {sample_dt!r}")

    return {
        "waypoints": waypoints,
        "max_velocity": _positive_float(constraints, "max_velocity"),
        "max_acceleration": _positive_float(constraints, "max_acceleration"),
        "max_lateral_acceleration": _positive_float(constraints, "max_lateral_acceleration"),
        "corner_strategy": strategy,
        "start_velocity": float(data.get("start_velocity", 0.0)),
        "end_velocity": float(data.get("end_velocity", 0.0)),
        "sample_dt": float(sample_dt) if sample_dt is not None else None,
    }


def run_request(request: dict) -> dict:
    """Execute a validated request and return the JSON-able response."""
    sample_dt = request.pop("sample_dt")
    try:
        result = parameterize(**request)
    except ValueError as exc:
        raise RequestError(str(exc)) from exc

    response: dict[str, Any] = {
        "total_time": result.total_time,
        "node_velocities": result.node_velocities.tolist(),
        "segment_durations": result.segment_durations.tolist(),
        "waypoint_times": result.waypoint_times.tolist(),
        "corner_velocities": {str(k): v for k, v in result.corner_velocities.items()},
        "meta": {
            "num_waypoints_input": len(request["waypoints"]),
            "num_waypoints_used": int(len(result.points)),
            "removed_duplicate_points": result.removed_duplicate_points,
            "corner_strategy": request["corner_strategy"],
        },
    }
    if sample_dt is not None:
        samples = sample_trajectory(
            result,
            max_acceleration=request["max_acceleration"],
            max_velocity=request["max_velocity"],
            dt=sample_dt,
        )
        response["samples"] = {
            "dt": sample_dt,
            "t": samples["t"].tolist(),
            "positions": np.round(samples["positions"], 9).tolist(),
            "speeds": samples["speeds"].tolist(),
            "accelerations": samples["accelerations"].tolist(),
        }
    return response


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) > 2:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        if argv:
            with open(argv[0], "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        else:
            raw = json.load(sys.stdin)
        request = parse_request(raw)
        response = run_request(request)
    except (RequestError, json.JSONDecodeError, OSError) as exc:
        json.dump({"error": str(exc)}, sys.stderr, ensure_ascii=False)
        sys.stderr.write("\n")
        return 2

    payload = json.dumps(response, ensure_ascii=False, indent=2)
    if len(argv) == 2:
        with open(argv[1], "w", encoding="utf-8") as handle:
            handle.write(payload + "\n")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
