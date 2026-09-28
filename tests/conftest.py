"""Shared pytest fixtures and helpers."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.executor import execute  # noqa: E402
from app.core.memory import simulate  # noqa: E402
from app.core.planner import CheckpointPlan, plan_checkpoints  # noqa: E402
from app.fixtures.graphs import Fixture, get_fixture  # noqa: E402
from app.fixtures.specs import ALL_SPECS  # noqa: E402
from app.reference.oracle import OracleGraph  # noqa: E402
from app.reference.seedspec import reference_masks  # noqa: E402

MASTER_SEED = 1234


@pytest.fixture
def linear_chain() -> Fixture:
    return get_fixture("linear_chain")


@pytest.fixture
def branching() -> Fixture:
    return get_fixture("branching")


@pytest.fixture
def tight_budget() -> Fixture:
    return get_fixture("tight_budget")


def make_plan(fx: Fixture, retained: list[str], strategy: str = "counter"
              ) -> CheckpointPlan:
    """Build a plan object for an arbitrary retained set (for tests)."""

    g = fx.graph()
    retained_full = list(dict.fromkeys(retained + [g.target]))
    sim = simulate(g, retained, rng_strategy=strategy)
    return CheckpointPlan(
        retained=tuple(retained_full),
        simulation=sim,
        search="exhaustive",
        candidates_evaluated=1,
        memory_budget=None,
        min_achievable_peak=sim.peak_memory,
    )


def run_fixture(fx: Fixture, retained: list[str] | None = None,
                strategy: str = "counter", seed: int = MASTER_SEED):
    g = fx.graph()
    if retained is None:
        plan = plan_checkpoints(g, None, rng_strategy=strategy)
    else:
        plan = make_plan(fx, retained, strategy)
    state = fx.training_state()
    return execute(g, plan, state, fx.inputs, run_id="test-run",
                   master_seed=seed, rng_strategy=strategy)


def oracle_answers(fx: Fixture, strategy: str = "counter",
                   seed: int = MASTER_SEED):
    spec = ALL_SPECS[fx.name]
    state = fx.training_state()
    params = {pid: state.get(pid) for pid in state.parameter_ids()}
    masks = reference_masks(strategy, seed, spec)
    param_grads, input_grads, loss = OracleGraph(spec).backward(
        params, fx.inputs, masks
    )
    return params, masks, param_grads, input_grads, loss
