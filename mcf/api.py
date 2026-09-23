"""JSON request/response layer for the minimum-cost flow service.

Request schema (see examples/ for concrete files)::

    {
      "n": <int, 2..256>,
      "source": <int>,
      "sink": <int>,
      "edges": [
        {"u": int, "v": int, "capacity": int, "cost": int}, ...
      ],
      "max_flow_limit": <optional non-negative int>
    }

Success response::

    {
      "status": "optimal",
      "flow": int,
      "cost": int,
      "sink_reachable": bool,
      "edge_flows": [int, ...],          // aligned 1:1 with request edges
      "potential": [int, ...]            // final potential per node
    }

Failure response::

    {"status": "invalid_request" | "negative_cycle" | "iteration_limit"
     | "internal_error", "error": "...message..."}
"""

from __future__ import annotations

import json
from typing import Any

from .solver import (
    CAP_MAX,
    COST_MAX,
    COST_MIN,
    INF,
    MAX_M,
    MAX_N,
    IterationLimitError,
    MCFError,
    NegativeCycleError,
    min_cost_max_flow,
)

_STATUS_INVALID = "invalid_request"
_STATUS_NEG_CYCLE = "negative_cycle"
_STATUS_ITER_LIMIT = "iteration_limit"
_STATUS_INTERNAL = "internal_error"


class InvalidRequest(ValueError):
    """The JSON request does not match the schema or value contract."""


def _as_int(value: Any, what: str) -> int:
    # Reject bool (bool is a subclass of int), floats, strings.
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidRequest(f"{what} must be an integer, got {type(value).__name__}")
    return value


def parse_request(req: Any) -> tuple[int, int, int, list[tuple[int, int, int, int]], int | None]:
    """Validate a decoded JSON request and return solver arguments."""
    if not isinstance(req, dict):
        raise InvalidRequest("request body must be a JSON object")

    for key in ("n", "source", "sink", "edges"):
        if key not in req:
            raise InvalidRequest(f"missing field: {key!r}")

    n = _as_int(req["n"], "n")
    if not (2 <= n <= MAX_N):
        raise InvalidRequest(f"n must satisfy 2 <= n <= {MAX_N}, got {n}")

    source = _as_int(req["source"], "source")
    sink = _as_int(req["sink"], "sink")
    if not (0 <= source < n):
        raise InvalidRequest(f"source out of range: {source}")
    if not (0 <= sink < n):
        raise InvalidRequest(f"sink out of range: {sink}")
    if source == sink:
        raise InvalidRequest("source and sink must be different nodes")

    raw_edges = req["edges"]
    if not isinstance(raw_edges, list):
        raise InvalidRequest("edges must be a list")
    if len(raw_edges) > MAX_M:
        raise InvalidRequest(f"too many edges: {len(raw_edges)} > {MAX_M}")

    edges: list[tuple[int, int, int, int]] = []
    for i, e in enumerate(raw_edges):
        if not isinstance(e, dict):
            raise InvalidRequest(f"edges[{i}] must be an object")
        for key in ("u", "v", "capacity", "cost"):
            if key not in e:
                raise InvalidRequest(f"edges[{i}] missing field: {key!r}")
        u = _as_int(e["u"], f"edges[{i}].u")
        v = _as_int(e["v"], f"edges[{i}].v")
        cap = _as_int(e["capacity"], f"edges[{i}].capacity")
        cost = _as_int(e["cost"], f"edges[{i}].cost")
        if not (0 <= u < n):
            raise InvalidRequest(f"edges[{i}].u out of range: {u}")
        if not (0 <= v < n):
            raise InvalidRequest(f"edges[{i}].v out of range: {v}")
        if u == v:
            # Self loops never carry useful s-t flow; reject outright to
            # make "negative self loop" a clear validation error rather
            # than a negative-cycle error.
            raise InvalidRequest(f"edges[{i}]: self loops are not allowed")
        if not (0 <= cap <= CAP_MAX):
            raise InvalidRequest(
                f"edges[{i}].capacity must satisfy 0 <= capacity <= 2^63-1"
            )
        if not (COST_MIN <= cost <= COST_MAX):
            raise InvalidRequest(
                f"edges[{i}].cost must satisfy 2^31-1 bounds ({COST_MIN}..{COST_MAX})"
            )
        edges.append((u, v, cap, cost))

    limit = None
    if "max_flow_limit" in req and req["max_flow_limit"] is not None:
        limit = _as_int(req["max_flow_limit"], "max_flow_limit")
        if limit < 0:
            raise InvalidRequest("max_flow_limit must be non-negative")

    return n, source, sink, edges, limit


def solve_request(req: Any) -> dict:
    """Validate and solve a decoded JSON request, returning a response dict."""
    try:
        n, source, sink, edges, limit = parse_request(req)
    except InvalidRequest as exc:
        return {"status": _STATUS_INVALID, "error": str(exc)}

    try:
        result = min_cost_max_flow(n, source, sink, edges, limit)
    except NegativeCycleError as exc:
        return {"status": _STATUS_NEG_CYCLE, "error": str(exc)}
    except IterationLimitError as exc:
        return {"status": _STATUS_ITER_LIMIT, "error": str(exc)}
    except MCFError as exc:
        return {"status": _STATUS_INTERNAL, "error": str(exc)}

    # Echo per-edge detail alongside the aligned flow vector.
    response = result.to_dict()
    # Unreachable nodes carry the internal INF sentinel; expose null so
    # the wire format never leaks the implementation-specific large int.
    response["potential"] = [
        None if p >= INF else p for p in result.potential
    ]
    response["edges"] = [
        {"u": u, "v": v, "capacity": cap, "cost": cost, "flow": flow}
        for (u, v, cap, cost), flow in zip(edges, result.edge_flows)
    ]
    return response


def solve_json_text(text: str) -> str:
    """Parse JSON text, solve, and return pretty-printed response JSON."""
    try:
        req = json.loads(text)
    except json.JSONDecodeError as exc:
        resp = {"status": _STATUS_INVALID, "error": f"malformed JSON: {exc}"}
    else:
        resp = solve_request(req)
    return json.dumps(resp, ensure_ascii=False, indent=2)
