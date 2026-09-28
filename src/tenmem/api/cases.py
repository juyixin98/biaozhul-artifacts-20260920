"""Named synthetic scenarios exposed by the HTTP API.

Each case returns ``(graph, feeds)``; ``shape_mutate`` takes ``n`` (actual
extent) and ``max_n`` (declared upper bound) so the same builder demonstrates
in-bound execution and over-bound growth that forces a replan.
"""
from __future__ import annotations

from ..errors import GraphValidationError
from .. import fixtures as fx


def build_case(name: str, params: dict | None = None):
    p = dict(params or {})
    try:
        if name == "diamond":
            n = int(p.get("n", 8))
            return fx.build_diamond(n), fx.diamond_feeds(n)
        if name == "long_lived":
            n, tail = int(p.get("n", 16)), int(p.get("tail", 4))
            g = fx.build_long_lived(n, tail)
            return g, fx.long_lived_feeds(n)
        if name == "shape_mutate":
            capacity = int(p.get("capacity", 64))
            bound_n = int(p.get("bound_n", 32))
            n = int(p.get("n", 10))
            return fx.build_shape_mutate(capacity, bound_n), fx.shape_mutate_feeds(n, capacity)
        if name == "parallel_branches":
            n, b = int(p.get("n", 12)), int(p.get("branches", 3))
            return fx.build_parallel_branches(n, b), fx.parallel_feeds(n)
        if name == "alias_chain":
            n = int(p.get("n", 8))
            return fx.build_alias(n), fx.alias_feeds(n)
        if name == "workspace_matmul":
            m, k, nn = int(p.get("m", 4)), int(p.get("k", 5)), int(p.get("n", 6))
            return fx.build_workspace_matmul(m, k, nn), fx.workspace_feeds(m, k, nn)
    except (TypeError, ValueError) as exc:
        raise GraphValidationError(
            f"invalid case parameters for {name!r}: {exc}",
            details={"case": name, "params": p},
        ) from exc
    raise GraphValidationError(
        f"unknown case {name!r}", details={"case": name, "known": list(CASE_NAMES)}
    )


CASE_NAMES = (
    "diamond",
    "long_lived",
    "shape_mutate",
    "parallel_branches",
    "alias_chain",
    "workspace_matmul",
)
