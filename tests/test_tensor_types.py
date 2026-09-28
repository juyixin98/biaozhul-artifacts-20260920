"""Unit tests for stable tensor identity and canonical digests."""

from __future__ import annotations

import numpy as np
import pytest

from adam_shard.tensor_types import TensorId, array_digest, json_digest


def test_tensor_identity_is_name_and_shape_not_position():
    a = TensorId("layers.0.weight", (5, 3))
    b = TensorId("layers.0.weight", (5, 3))
    c = TensorId("layers.1.weight", (5, 3))
    assert a == b
    assert a != c
    assert hash(a) == hash(b)
    assert a.numel == 15


def test_tensor_id_rejects_bad_shapes_and_names():
    with pytest.raises(ValueError):
        TensorId("", (2,))
    with pytest.raises(ValueError):
        TensorId("x", (0, 3))
    with pytest.raises(ValueError):
        TensorId("x", (-1,))


def test_digest_independent_of_memory_layout():
    base = np.arange(12, dtype=np.float64).reshape(3, 4)
    transposed = base.T.copy().T  # different strides, same values
    assert array_digest(base) == array_digest(transposed)
    assert array_digest(base) == array_digest(base.astype(np.float32))


def test_digest_changes_when_values_change():
    a = np.zeros(4, dtype=np.float64)
    b = a.copy()
    b[2] = 1e-12
    assert array_digest(a) != array_digest(b)


def test_json_digest_order_independent_but_value_sensitive():
    assert json_digest({"a": 1, "b": 2}) == json_digest({"b": 2, "a": 1})
    assert json_digest({"a": 1}) != json_digest({"a": 2})
