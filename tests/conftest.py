"""Pytest bootstrap and shared fixtures: make ``src/`` importable."""

import sys
from pathlib import Path

import numpy as np
import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
for extra in (SRC, PROJECT_ROOT):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from amptrain.config import (  # noqa: E402
    AccumulationConfig,
    DataConfig,
    ModelConfig,
    OptimizerConfig,
    PrecisionConfig,
    RunConfig,
    ScalerConfig,
)
from amptrain.data import make_fixture  # noqa: E402
from amptrain.trainer import MixedPrecisionTrainer  # noqa: E402
from tests.helpers import make_config  # noqa: E402


@pytest.fixture
def config() -> RunConfig:
    return make_config()


@pytest.fixture
def fixture(config):
    return make_fixture(config.data, n_samples=128, amplification=1.0)


@pytest.fixture
def trainer(config, fixture):
    return MixedPrecisionTrainer(config, run_id="test-run", fixture=fixture)
