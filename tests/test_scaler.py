"""Dynamic loss scaler state machine."""

import math

import numpy as np

from amptrain.config import ScalerConfig
from amptrain.scaler import LossScaler

FP16_MAX = float(np.finfo(np.float16).max)


def _scaler(**kw):
    return LossScaler.initial(ScalerConfig(**kw), FP16_MAX)


def test_initial_scale_and_inverse():
    s = _scaler(init_scale=128.0)
    assert s.scale == 128.0
    assert math.isclose(s.inv_scale, 1.0 / 128.0)


def test_overflow_backs_off_and_resets_growth_tracker():
    s = _scaler(init_scale=128.0, backoff_factor=0.5)
    grown = s.record_good_step(FP16_MAX)
    assert grown.growth_tracker == 1
    backed = grown.record_overflow()
    assert backed.scale == 64.0
    assert backed.growth_tracker == 0  # streak reset, scale halved


def test_repeated_overflow_converges_to_min_scale_not_zero():
    s = _scaler(init_scale=8.0, backoff_factor=0.5, min_scale=1.0)
    for _ in range(10):
        s = s.record_overflow()
    assert s.scale == 1.0  # never collapses to 0
    assert math.isfinite(s.scale)


def test_growth_only_after_interval_of_committed_good_steps():
    s = _scaler(init_scale=4.0, growth_factor=2.0, growth_interval=3)
    s = s.record_good_step(FP16_MAX)
    s = s.record_good_step(FP16_MAX)
    assert s.scale == 4.0  # not yet
    s = s.record_good_step(FP16_MAX)
    assert s.scale == 8.0 and s.growth_tracker == 0


def test_scale_never_exceeds_lowp_max():
    s = _scaler(init_scale=FP16_MAX, growth_factor=2.0, growth_interval=1)
    s = s.record_good_step(FP16_MAX)
    assert s.scale == FP16_MAX


def test_state_roundtrip_preserves_scale_and_tracker():
    s = _scaler(init_scale=32.0).record_overflow()
    restored = LossScaler.from_state(ScalerConfig(), s.to_state())
    assert restored.scale == s.scale == 16.0
    assert restored.growth_tracker == 0


def test_init_scale_above_dtype_range_is_rejected():
    import pytest
    with pytest.raises(ValueError):
        LossScaler.initial(ScalerConfig(init_scale=1e9), FP16_MAX)
