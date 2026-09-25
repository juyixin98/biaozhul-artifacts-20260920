"""Executor equivalence tests (no-reuse baseline vs arena reuse)."""

import numpy as np

from tmem.dag import build_dag
from tmem.executor import (
    compare_results,
    execute,
    execute_no_reuse,
    execute_reuse,
    make_inputs,
)
from tmem.planner import plan_memory

from helpers import assert_outputs_match, assert_plan_safe


def test_basic_chain_numeric_equivalence():
    dag = build_dag(
        {
            "inputs": {"x0": [8, 8], "w": [8, 8]},
            "ops": [
                {"name": f"x{i}", "op": "matmul",
                 "inputs": [f"x{i-1}", "w"]}
                for i in range(1, 5)
            ],
            "outputs": ["x4"],
        }
    )
    run = execute(dag, mode="both", seed=143)
    assert_outputs_match(run)
    assert run["results"]["reuse"].peak_elements < run["results"][
        "no_reuse"
    ].peak_elements


def test_reuse_executor_writes_into_single_arena():
    dag = build_dag(
        {
            "inputs": {"x": [4, 6], "w": [3, 2], "b": [4, 2]},
            "ops": [
                {"name": "v", "op": "slice", "inputs": ["x"],
                 "starts": [0, 1], "stops": [4, 4]},
                {"name": "t", "op": "matmul", "inputs": ["v", "w"]},
                {"name": "u", "op": "add", "inputs": ["t", "b"]},
            ],
            "outputs": ["u"],
        }
    )
    plan = plan_memory(dag)
    assert_plan_safe(plan)
    res = execute_reuse(dag, plan=plan)
    base = execute_no_reuse(dag)
    cmp_ = compare_results(base.outputs, res.outputs)
    assert cmp_["match"], cmp_
    assert res.allocations == 1
    assert base.allocations == 6  # 3 inputs + 3 ops


def test_broadcast_add_equivalence():
    dag = build_dag(
        {
            "inputs": {"a": [2, 3, 4], "b": [4]},
            "ops": [
                {"name": "c", "op": "add", "inputs": ["a", "b"]},
                {"name": "d", "op": "add", "inputs": ["c", "b"]},
            ],
            "outputs": ["d"],
        }
    )
    assert_outputs_match(execute(dag, mode="both"))


def test_reuse_peak_is_never_higher_than_baseline():
    request = {
        "inputs": {"X": [4, 6], "W": [3, 2], "bias": [3, 3], "c": [2]},
        "ops": [
            {"name": "v", "op": "slice", "inputs": ["X"],
             "starts": [1, 2], "stops": [4, 5]},
            {"name": "v2", "op": "slice", "inputs": ["X"],
             "starts": [0, 0], "stops": [3, 3]},
            {"name": "t1", "op": "matmul", "inputs": ["v", "W"]},
            {"name": "t3", "op": "add", "inputs": ["v", "v2"]},
            {"name": "v3", "op": "slice", "inputs": ["v2"],
             "starts": [0, 0], "stops": [2, 2]},
            {"name": "t4", "op": "add", "inputs": ["t3", "bias"]},
            {"name": "t5", "op": "add", "inputs": ["t1", "c"]},
        ],
        "outputs": ["t4", "t5", "v3"],
    }
    dag = build_dag(request)
    run = execute(dag, mode="both", seed=2026)
    assert_outputs_match(run)
    reused = run["results"]["reuse"].peak_elements
    base = run["results"]["no_reuse"].peak_elements
    assert reused < base


def test_inputs_are_deterministic():
    dag = build_dag(
        {
            "inputs": {"a": [3, 3]},
            "ops": [{"name": "o", "op": "add", "inputs": ["a", "a"]}],
            "outputs": ["o"],
        }
    )
    i1 = make_inputs(dag, seed=143)
    i2 = make_inputs(dag, seed=143)
    i3 = make_inputs(dag, seed=999)
    assert np.array_equal(i1["a"], i2["a"])
    assert not np.array_equal(i1["a"], i3["a"])


def test_output_shapes_agree():
    dag = build_dag(
        {
            "inputs": {"a": [2, 3, 4, 5], "b": [2, 3, 5, 6]},
            "ops": [{"name": "y", "op": "matmul", "inputs": ["a", "b"]}],
            "outputs": ["y"],
        }
    )
    run = execute(dag, mode="both")
    assert run["results"]["no_reuse"].outputs["y"].shape == (2, 3, 4, 6)
    assert run["results"]["reuse"].outputs["y"].shape == (2, 3, 4, 6)
    assert_outputs_match(run)
