"""Unit tests for tensor identity, shapes and content hashing."""
from __future__ import annotations

import numpy as np
import pytest

from adam_shards.tensor_types import ParamId, Tensor, tensor_digest


def test_param_id_uses_name_and_shape_not_position():
    a = ParamId("layers.0.weight", (2, 3))
    b = ParamId("layers.0.weight", (2, 3))
    c = ParamId("layers.0.weight", (3, 2))
    assert a == b
    assert a != c  # same name, different shape is a different identity


def test_param_id_rejects_bad_identity():
    with pytest.raises(ValueError):
        ParamId("", (2,))
    with pytest.raises(ValueError):
        ParamId("x", (0, 3))


def test_tensor_enforces_declared_shape():
    pid = ParamId("w", (2, 2))
    with pytest.raises(ValueError, match="shape mismatch"):
        Tensor(pid, "param", np.zeros((3, 2)))


def test_tensor_bytes_roundtrip_preserves_values():
    pid = ParamId("w", (2, 3))
    arr = np.arange(6, dtype=np.float64).reshape(2, 3)
    t = Tensor(pid, "moment1", arr)
    back = Tensor.from_bytes(pid, "moment1", t.to_bytes())
    np.testing.assert_array_equal(back.data, arr)


def test_tensor_from_bytes_rejects_wrong_length():
    pid = ParamId("w", (4,))
    with pytest.raises(ValueError, match="byte length mismatch"):
        Tensor.from_bytes(pid, "param", np.zeros(3, dtype=np.float64).tobytes())


def test_digest_changes_when_values_or_identity_change():
    pid = ParamId("w", (3,))
    base = Tensor(pid, "param", np.array([1.0, 2.0, 3.0]))
    other_val = Tensor(pid, "param", np.array([1.0, 2.0, 4.0]))
    other_name = Tensor(ParamId("v", (3,)), "param", np.array([1.0, 2.0, 3.0]))
    assert tensor_digest(base) != tensor_digest(other_val)
    assert tensor_digest(base) != tensor_digest(other_name)
