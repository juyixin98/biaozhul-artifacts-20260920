"""Executor measured memory/cost must equal the planner simulation.

Every checkpoint subset is really executed with numpy.  The executor's
independently tracked peak memory and recompute cost must match the
simulation exactly, while gradients still match the independent oracle.
This prevents planner/executor accounting drift.
"""

from __future__ import annotations

from itertools import combinations

import numpy as np
import pytest

from app.core import ops as ops_mod
from app.core.executor import execute
from tests.conftest import make_plan, oracle_answers


def _internal_ids(fx) -> list[str]:
    g = fx.graph()
    return [
        nid for nid in g.order
        if g.nodes[nid].op not in ops_mod.ROOT_OPS and nid != g.target
    ]


@pytest.mark.integration
@pytest.mark.parametrize("strategy", ["counter", "snapshot"])
def test_measured_peak_and_costs_match_simulation_for_every_subset(
    linear_chain, strategy
) -> None:
    fx = linear_chain
    internal = _internal_ids(fx)
    _, _, pg, ig, loss = oracle_answers(fx, strategy)
    executed = 0
    for r in range(len(internal) + 1):
        for subset in combinations(internal, r):
            plan = make_plan(fx, list(subset), strategy)
            run, log = execute(
                fx.graph(), plan, fx.training_state(), fx.inputs,
                run_id=f"sub-{strategy}-{r}", master_seed=1234,
                rng_strategy=strategy,
            )
            sim = plan.simulation
            assert run.peak_memory == sim.peak_memory, subset
            assert run.recompute_flops == sim.recompute_flops, subset
            assert run.forward_flops == sim.forward_flops
            assert abs(run.loss - loss) <= 1e-10
            for pid in pg:
                assert np.allclose(run.grads[pid], pg[pid], atol=1e-10)
            executed += 1
    assert executed == 16


@pytest.mark.integration
@pytest.mark.parametrize("strategy", ["counter", "snapshot"])
def test_branching_every_subset_matches_simulation_and_oracle(
    branching, strategy
) -> None:
    fx = branching
    internal = _internal_ids(fx)
    _, _, pg, _, loss = oracle_answers(fx, strategy)
    count = 0
    for r in range(len(internal) + 1):
        for subset in combinations(internal, r):
            plan = make_plan(fx, list(subset), strategy)
            run, log = execute(
                fx.graph(), plan, fx.training_state(), fx.inputs,
                run_id="branch-sub", master_seed=1234, rng_strategy=strategy,
            )
            assert run.peak_memory == plan.simulation.peak_memory, subset
            assert run.recompute_flops == plan.simulation.recompute_flops
            assert abs(run.loss - loss) <= 1e-10
            assert all(
                np.allclose(run.grads[p], pg[p], atol=1e-10) for p in pg
            )
            count += 1
    assert count == 2 ** len(internal)


@pytest.mark.integration
def test_nonfinite_input_is_computation_input_error(linear_chain) -> None:
    from app.core.errors import InvalidInputError

    fx = linear_chain
    plan = make_plan(fx, [])
    bad_inputs = dict(fx.inputs)
    bad_inputs["x"] = bad_inputs["x"].copy()
    bad_inputs["x"][0, 0] = np.inf
    with pytest.raises(InvalidInputError) as exc:
        execute(fx.graph(), plan, fx.training_state(), bad_inputs,
                run_id="bad-input")
    assert exc.value.code == "E_TENSOR_NONFINITE"
