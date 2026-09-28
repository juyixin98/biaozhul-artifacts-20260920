"""Configuration override validation."""

from __future__ import annotations

import pytest

from app.config import Config

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def config() -> Config:
    return Config.load()


def test_unknown_config_key_rejected(config):
    with pytest.raises(ValueError, match="unknown configuration keys"):
        config.with_overrides({"residual_dps": 90})


def test_ladder_entries_below_four_digits_rejected(config):
    with pytest.raises(ValueError, match="must be >= 4"):
        config.with_overrides({"mp_dps_ladder": [30, 2]})


def test_iteration_budget_bounds_validated(config):
    with pytest.raises(ValueError, match="max_iterations"):
        config.with_overrides({"max_iterations_per_stage": 0})
    with pytest.raises(ValueError, match="max_iterations"):
        config.with_overrides({"max_iterations_per_stage": 1000})


def test_all_factorization_stages_cannot_be_disabled(config):
    with pytest.raises(ValueError, match="at least one factorization stage"):
        config.with_overrides(
            {"use_fp32_first": False, "use_fp64": False, "mp_dps_ladder": []}
        )


def test_empty_ladder_is_allowed_when_binary_stage_remains(config):
    resolved = config.with_overrides({"mp_dps_ladder": []})
    assert resolved.mp_dps_ladder == tuple()
