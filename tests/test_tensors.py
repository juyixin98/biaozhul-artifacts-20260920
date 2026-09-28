"""Tensor-type layer: the three-state separation invariant."""

import numpy as np

from amptrain.tensors import (
    GradientAccumulator,
    MasterWeights,
    OptimizerState,
    init_shapes,
    lowp_numpy_dtype,
)
from amptrain.config import PrecisionConfig


def test_master_lowp_optimizer_state_are_distinct_arrays(config):
    rng = np.random.default_rng(0)
    master = MasterWeights.initialize(config.model, rng)
    lowp = master.cast_to_lowp(lowp_numpy_dtype(config.precision))
    opt = OptimizerState.zeros(config.model)

    for name in ("W1", "W2"):
        # distinct dtype ...
        assert master.matrices[name].dtype == np.float32
        assert lowp.matrices[name].dtype == np.float16
        assert opt.momentum[name].dtype == np.float32
        # ... distinct memory (the cast is a copy, not a view) ...
        assert not np.shares_memory(master.matrices[name], lowp.matrices[name])
        assert not np.shares_memory(master.matrices[name], opt.momentum[name])
        # ... and the momentum buffer starts at zero independently.
        assert np.all(opt.momentum[name] == 0.0)


def test_mutating_lowp_cast_does_not_touch_master(config):
    rng = np.random.default_rng(0)
    master = MasterWeights.initialize(config.model, rng)
    lowp = master.cast_to_lowp(np.dtype(np.float16))
    before = master.matrices["W1"].copy()
    lowp.matrices["W1"].fill(0.0)  # simulate a rogue write into the cast
    assert np.array_equal(master.matrices["W1"], before)


def test_updated_returns_new_instance_and_leaves_inputs_untouched(config):
    rng = np.random.default_rng(0)
    master = MasterWeights.initialize(config.model, rng)
    deltas = {name: np.ones_like(master.matrices[name]) * np.float32(0.1)
              for name in ("W1", "W2")}
    original = {name: master.matrices[name].copy() for name in ("W1", "W2")}
    new_master = master.updated(deltas)

    assert new_master is not master
    for name in ("W1", "W2"):
        assert np.array_equal(master.matrices[name], original[name])  # old untouched
        assert np.array_equal(new_master.matrices[name], original[name] + np.float32(0.1))


def test_gradient_accumulator_is_functional_and_typed_fp32(config):
    acc = GradientAccumulator.empty(config.model)
    g = {name: np.ones(init_shapes(config.model)[name], dtype=np.float16)
         for name in ("W1", "W2")}
    acc2 = acc.add(g)

    assert acc.micro_batches_seen == 0  # original unchanged (no in-place mutation)
    assert acc2.micro_batches_seen == 1
    assert acc2.sums["W1"].dtype == np.float32
    np.testing.assert_array_equal(acc2.sums["W1"], np.ones_like(acc2.sums["W1"]))

    acc4 = acc2.add(g).add(g).add(g)
    avg = acc4.average(4)
    np.testing.assert_allclose(avg["W1"], np.ones_like(avg["W1"]), rtol=1e-6)


def test_lowp_dtype_resolves_and_is_floating():
    resolved = lowp_numpy_dtype(PrecisionConfig(lowp_dtype="float16"))
    assert resolved == np.dtype(np.float16)
    resolved32 = lowp_numpy_dtype(PrecisionConfig(lowp_dtype="float32"))
    assert resolved32 == np.dtype(np.float32)


def test_bfloat16_is_not_advertised_by_this_build():
    import pytest
    from amptrain.errors import AmpTrainError
    with pytest.raises(AmpTrainError):
        PrecisionConfig(lowp_dtype="bfloat16")
