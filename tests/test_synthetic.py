"""Unit tests for synthetic dataset reproducibility."""

import numpy as np
import pytest

from checkpoint_service.config import TrainConfig
from checkpoint_service.synthetic import make_dataset


@pytest.mark.unit
def test_dataset_is_bit_reproducible_from_seed() -> None:
    cfg = TrainConfig(data_seed=123)
    a = make_dataset(cfg)
    b = make_dataset(cfg)
    np.testing.assert_array_equal(a.X, b.X)
    np.testing.assert_array_equal(a.y, b.y)
    assert a.fingerprint == b.fingerprint


@pytest.mark.unit
def test_fingerprint_detects_tampering() -> None:
    ds = make_dataset(TrainConfig())
    ds.X[0, 0] += 1.0
    with pytest.raises(ValueError, match="fingerprint"):
        ds.verify()


@pytest.mark.unit
def test_distinct_data_and_train_seeds() -> None:
    # Changing train seed must not change the data; changing data seed must.
    base = make_dataset(TrainConfig(data_seed=1, train_seed=2))
    same_data = make_dataset(TrainConfig(data_seed=1, train_seed=99))
    other = make_dataset(TrainConfig(data_seed=2, train_seed=2))
    np.testing.assert_array_equal(base.X, same_data.X)
    assert not np.array_equal(base.X, other.X)
