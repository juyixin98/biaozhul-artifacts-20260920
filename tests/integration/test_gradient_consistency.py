"""Phase-3 case A: fixed-dropout + branch graph gradient consistency.

The dropout mask fixture is *fixed* by seed and independently specified in
``app.reference.seedspec``.  Gradients must agree with the independent eager
oracle for every RNG strategy and across plans that retain vs recompute the
stochastic activation.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.fixtures.specs import ALL_SPECS
from app.reference.finite_diff import finite_difference_param_grads
from tests.conftest import make_plan, oracle_answers

GRAD_ATOL = 1e-10


@pytest.mark.integration
@pytest.mark.parametrize("strategy", ["counter", "snapshot"])
@pytest.mark.parametrize("retained_dropout", [True, False])
def test_branch_gradients_match_independent_oracle(
    branching, strategy: str, retained_dropout: bool
) -> None:
    fx = branching
    retained = ["d1"] if retained_dropout else []
    plan = make_plan(fx, retained, strategy)
    state = fx.training_state()
    run, log = _execute(fx, plan, state, strategy)

    _, _, pg, ig, loss = oracle_answers(fx, strategy)
    assert abs(run.loss - loss) <= GRAD_ATOL
    for pid, expected in pg.items():
        assert np.allclose(run.grads[pid], expected, atol=GRAD_ATOL), pid
    for iid, expected in ig.items():
        assert np.allclose(np.asarray(log["input_grads"][iid]), expected,
                           atol=GRAD_ATOL), iid


@pytest.mark.integration
@pytest.mark.parametrize("strategy", ["counter", "snapshot"])
def test_dropout_mask_is_replayed_not_resampled(branching, strategy) -> None:
    # Force the dropout to be recomputed (no retained activation).
    plan = make_plan(branching, [], strategy)
    state = branching.training_state()
    run, log = execute_with(branching, plan, state, strategy)
    assert run.status.value == "executed"
    # The replay log shows d1 replayed in exactly one wave.
    replayed = [
        n for wave in log["replay_waves"] for n in wave["recomputed"]
    ]
    mask_replays = [
        n for wave in log["replay_waves"] for n in wave["mask_replays"]
    ]
    assert (replayed.count("d1") + mask_replays.count("d1")) == 1


@pytest.mark.integration
@pytest.mark.parametrize("strategy", ["counter", "snapshot"])
def test_external_side_effect_fires_once_even_with_replay(
    branching, strategy
) -> None:
    # `ext` is an internal node feeding branchA; it is dropped and replayed,
    # but its external emit must happen exactly once (in the forward).
    plan = make_plan(branching, [], strategy)
    state = branching.training_state()
    run, log = execute_with(branching, plan, state, strategy)
    assert run.emitted_side_effects == 1
    assert log["external_emits"] == ["ext"]
    suppressed = sum(w["emits_suppressed"] for w in log["replay_waves"])
    assert suppressed >= 1


@pytest.mark.integration
def test_gradients_survive_a_training_step_without_drift(branching) -> None:
    fx = branching
    plan = make_plan(fx, [])
    state = fx.training_state()
    run, _ = execute_with(fx, plan, state, "counter")
    before = state.get("W1").copy()
    state.apply_run(run, lr=0.05)
    assert state.step == 1
    assert np.allclose(state.get("W1"), before - 0.05 * run.grads["W1"])


@pytest.mark.integration
@pytest.mark.parametrize("strategy", ["counter", "snapshot"])
def test_finite_difference_confirms_oracle_and_executor(
    branching, strategy
) -> None:
    fx = branching
    plan = make_plan(fx, [], strategy)
    state = fx.training_state()
    run, _ = execute_with(fx, plan, state, strategy)
    params, masks, pg, _, _ = oracle_answers(fx, strategy)
    fd = finite_difference_param_grads(ALL_SPECS[fx.name], params,
                                       fx.inputs, masks, eps=1e-6)
    # executor vs finite difference
    for pid in fd:
        assert np.max(np.abs(run.grads[pid] - fd[pid])) < 1e-5
    # independent eager autodiff vs finite difference
    for pid in fd:
        assert np.max(np.abs(pg[pid] - fd[pid])) < 1e-5


# --------------------------------------------------------------------------
# helpers (kept local so the test reads top-to-bottom without conftest magic)
# --------------------------------------------------------------------------


def execute_with(fx, plan, state, strategy: str, seed: int = 1234):
    from app.core.executor import execute

    return execute(fx.graph(), plan, state, fx.inputs, run_id="t-branch",
                   master_seed=seed, rng_strategy=strategy)


def _execute(fx, plan, state, strategy: str, seed: int = 1234):
    return execute_with(fx, plan, state, strategy, seed)
