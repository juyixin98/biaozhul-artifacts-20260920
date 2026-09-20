"""Step dependency validation and version content hashing.

A version is an ordered list of at most 50 steps. Steps reference their
prerequisites by 1-based position. A version is publishable only when:

* every prerequisite references an existing step (no dangling dependency);
* a step does not depend on itself;
* the dependency graph is acyclic (Kahn topological sort drains fully).
"""
from __future__ import annotations

import hashlib
import json
from collections import deque

from ..schemas import StepIn

MAX_STEPS = 50


class GraphError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def validate_steps(steps: list[StepIn]) -> None:
    n = len(steps)
    if n == 0:
        raise GraphError("STEPS_EMPTY", "a program version needs at least one step")
    if n > MAX_STEPS:
        raise GraphError("STEPS_TOO_MANY", f"a program version may contain at most {MAX_STEPS} steps")

    valid_orders = set(range(1, n + 1))
    edges: dict[int, set[int]] = {i: set() for i in valid_orders}

    for idx, step in enumerate(steps, start=1):
        if not step.title.strip():
            raise GraphError("STEP_TITLE_EMPTY", f"step {idx}: title is required")
        for pre in step.prerequisite_orders:
            if pre not in valid_orders:
                raise GraphError(
                    "PREREQUISITE_UNKNOWN",
                    f"step {idx} references unknown prerequisite step {pre}",
                )
            if pre == idx:
                raise GraphError(
                    "PREREQUISITE_SELF", f"step {idx} cannot be its own prerequisite"
                )
            edges[idx].add(pre)

    # Kahn topological sort; anything left over participates in a cycle.
    indegree = {i: 0 for i in valid_orders}
    dependents: dict[int, list[int]] = {i: [] for i in valid_orders}
    for step_order, pres in edges.items():
        indegree[step_order] = len(pres)
        for pre in pres:
            dependents[pre].append(step_order)

    queue = deque([i for i in valid_orders if indegree[i] == 0])
    seen = 0
    while queue:
        node = queue.popleft()
        seen += 1
        for dep in dependents[node]:
            indegree[dep] -= 1
            if indegree[dep] == 0:
                queue.append(dep)

    if seen != n:
        cyclic = sorted(i for i, indeg in indegree.items() if indeg > 0)
        raise GraphError(
            "STEPS_CYCLIC",
            f"dependency cycle detected involving steps {cyclic}",
        )


def content_hash_for(steps: list[StepIn]) -> str:
    """Stable SHA-256 over exactly what a version pins: ordered step content."""
    payload = [
        {
            "order": i,
            "title": s.title,
            "description": s.description,
            "pass_criteria": s.pass_criteria,
            "prerequisites": sorted(s.prerequisite_orders),
        }
        for i, s in enumerate(steps, start=1)
    ]
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
