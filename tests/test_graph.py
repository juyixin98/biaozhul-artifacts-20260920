"""Compute graph: staged finite checks and low-precision execution."""

import numpy as np

from amptrain import graph
from amptrain.config import PrecisionConfig
from amptrain.tensors import lowp_numpy_dtype

from tests.helpers import amplified_batches, make_config, normal_batches


def _params_for(config, seed=1):
    rng = np.random.default_rng(seed)
    from amptrain.tensors import MasterWeights
    master = MasterWeights.initialize(config.model, rng)
    return master.cast_to_lowp(lowp_numpy_dtype(config.precision))


def test_forward_runs_in_low_precision_and_passes_finite_check(config):
    x, y = normal_batches(config, 1)[0]
    params = _params_for(config)
    cache, overflow = graph.forward(params, x, config.model.activation)
    assert not overflow.overflowed
    assert cache.y.dtype == np.float16
    assert cache.x.dtype == np.float16
    assert np.all(np.isfinite(cache.y))


def test_amplified_input_is_detected_at_a_specific_stage(config):
    # Calibration (fp16 max 65504): amp=1000 gives forward outputs ~O(1e3)
    # which stay finite, while loss ~ O(1e6) x scale 128 ~ O(1e8) overflows.
    x, y = amplified_batches(config, amplification=1000.0, n=1)[0]
    params = _params_for(config)

    cache, ov_fwd = graph.forward(params, x, config.model.activation)
    assert not ov_fwd.overflowed, "forward activations must remain finite at amp=1000"
    assert np.all(np.isfinite(cache.y))

    loss_scaled, d_y, ov_loss = graph.scaled_mse_loss(cache, y, scale=128.0)
    assert ov_loss.overflowed
    assert ov_loss.stage == graph.STAGE_SCALED_LOSS
    assert "loss_scaled" in ov_loss.nonfinite_tensors
    assert not np.isfinite(loss_scaled)
    # The trainer contract: a classified overflow is never turned into an
    # optimizer update.


def test_larger_amplification_overflows_already_in_forward_stage(config):
    # amp=1e5 pushes hidden activations themselves beyond fp16 range.
    x, y = amplified_batches(config, amplification=100_000.0, n=1)[0]
    params = _params_for(config)
    _, ov_fwd = graph.forward(params, x, config.model.activation)
    assert ov_fwd.overflowed
    assert ov_fwd.stage == graph.STAGE_FORWARD
    assert ov_fwd.nonfinite_tensors  # offending tensor named, not a generic failure


def test_find_nonfinite_classifies_nan_and_inf():
    assert graph.find_nonfinite({"a": np.array([1.0]), "b": np.array([np.inf])}) == ["b"]
    assert graph.find_nonfinite({"a": np.array([np.nan])}) == ["a"]
    assert graph.find_nonfinite({"a": np.array([1.0, 2.0])}) == []


def test_fp32_logging_loss_is_independent_of_lowp_path(config):
    from amptrain.tensors import MasterWeights
    rng = np.random.default_rng(1)
    master = MasterWeights.initialize(config.model, rng)
    x, y = normal_batches(config, 1)[0]
    loss = graph.loss_fp32(master.matrices, x, y, config.model.activation)
    assert np.isfinite(loss) and loss > 0.0
    # Same amplified input that ruins the lp path gives a finite fp32 loss.
    x_big, y_big = amplified_batches(config, 100.0, 1)[0]
    loss_big = graph.loss_fp32(master.matrices, x_big, y_big, config.model.activation)
    assert np.isfinite(loss_big) and loss_big > loss


def test_float32_control_config_supported():
    cfg = make_config(precision=PrecisionConfig(lowp_dtype="float32"))
    assert lowp_numpy_dtype(cfg.precision) == np.dtype(np.float32)
