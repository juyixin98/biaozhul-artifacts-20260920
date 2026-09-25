"""Unit tests for TrainConfig validation."""

import pytest

from checkpoint_service.config import TrainConfig


@pytest.mark.unit
@pytest.mark.parametrize(
    "kwargs",
    [
        {"n_samples": 0},
        {"n_features": -1},
        {"batch_size": 0},
        {"batch_size": 1000},  # exceeds n_samples
        {"lr": 0.0},
        {"momentum": 1.0},
        {"momentum": -0.1},
        {"l2": -1.0},
        {"n_epochs": 0},
        {"checkpoint_every": -2},
    ],
)
def test_invalid_config_rejected(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        TrainConfig(**kwargs)


@pytest.mark.unit
def test_config_dict_roundtrip() -> None:
    cfg = TrainConfig(n_epochs=3)
    assert TrainConfig.from_dict(cfg.to_dict()) == cfg
    with pytest.raises(ValueError):
        TrainConfig.from_dict({"n_samples": 1})  # missing fields


@pytest.mark.unit
def test_steps_per_epoch_and_total() -> None:
    cfg = TrainConfig(n_samples=100, batch_size=16, n_epochs=3)
    assert cfg.steps_per_epoch == 6  # trailing partial batch dropped
    assert cfg.total_steps == 18
