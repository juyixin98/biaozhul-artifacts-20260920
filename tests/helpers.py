"""Shared test helpers."""

import numpy as np

from tmem.dag import build_dag
from tmem.executor import execute
from tmem.planner import overlapping_live_pairs, plan_memory


def run_request(request: dict):
    dag = build_dag(request)
    plan = plan_memory(dag)
    run = execute(dag, mode="both", seed=request.get("seed", 143))
    return dag, plan, run


def assert_plan_safe(plan):
    assert overlapping_live_pairs(plan) == []
    # Every view's touched region stays inside its root window. An empty
    # slice view has an empty footprint where lo == hi.
    for view in plan.views.values():
        root = plan.buffer_for(view.root)
        lo, hi = view.absolute_footprint_bounds(root.offset)
        assert root.offset <= lo <= hi <= root.offset + root.size, (
            view.name,
            lo,
            hi,
            root,
        )


def assert_outputs_match(run):
    cmp_ = run["comparison"]
    assert cmp_["match"], cmp_
    for name, stats in cmp_["outputs"].items():
        assert stats["allclose"], (name, stats)
