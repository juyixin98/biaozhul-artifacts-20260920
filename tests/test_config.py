"""Configuration validation at the system boundary."""

import pytest

from amptrain.config import (
    AccumulationConfig,
    DataConfig,
    ModelConfig,
    OptimizerConfig,
    PrecisionConfig,
    RunConfig,
    ScalerConfig,
)
from amptrain.errors import AmpTrainError, ErrorCode


@pytest.mark.parametrize(
    "kwargs",
    [
        {"init_scale": 0.0},
        {"init_scale": float("nan")},
        {"growth_factor": 1.0},
        {"backoff_factor": 1.0},
        {"backoff_factor": 0.0},
        {"growth_interval": 0},
        {"min_scale": 0.0},
    ],
)
def test_bad_scaler_config_rejected(kwargs):
    with pytest.raises(AmpTrainError) as excinfo:
        ScalerConfig(**kwargs)
    assert excinfo.value.code == ErrorCode.CONFIG_INVALID


@pytest.mark.parametrize("dtype", ["float64", "int16", "half"])
def test_bad_lowp_dtype_rejected(dtype):
    with pytest.raises(AmpTrainError):
        PrecisionConfig(lowp_dtype=dtype)


@pytest.mark.parametrize("lr", [0.0, -0.1, float("inf")])
def test_bad_lr_rejected(lr):
    with pytest.raises(AmpTrainError):
        OptimizerConfig(lr=lr)


def test_micro_batches_must_be_positive():
    with pytest.raises(AmpTrainError):
        AccumulationConfig(micro_batches=0)


def test_feature_dim_mismatch_rejected():
    with pytest.raises(AmpTrainError) as excinfo:
        RunConfig(
            model=ModelConfig(in_dim=3),
            data=DataConfig(n_features=4),
        )
    assert excinfo.value.code == ErrorCode.CONFIG_INVALID


def test_valid_config_is_frozen():
    cfg = ScalerConfig()
    with pytest.raises(Exception):
        cfg.init_scale = 9.0  # type: ignore[misc]
