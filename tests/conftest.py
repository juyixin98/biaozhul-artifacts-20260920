"""Pytest fixtures shared across the test suite."""
import sys
from pathlib import Path

import numpy as np
import pytest

# Make the package importable when tests are run from a plain checkout.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from grouped_splitter import generate_synthetic_dataset, split_dataset  # noqa: E402


@pytest.fixture
def default_dataset():
    return generate_synthetic_dataset(seed=42)


@pytest.fixture
def default_result(default_dataset):
    return split_dataset(
        groups=list(default_dataset.group_ids),
        labels=list(default_dataset.y),
        seed=42,
    )


@pytest.fixture
def rng():
    return np.random.default_rng(123)
