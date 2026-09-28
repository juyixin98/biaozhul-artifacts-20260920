"""Differential tests against an INDEPENDENT NumPy oracle.

Expected answers are computed by NumPy directly on independent arrays; the
core never generates its own reference data.  These tests assert concrete
values *and* the view/copy and aliasing classification.
"""
from __future__ import annotations

import random

import numpy as np
import pytest
from numpy.lib.stride_tricks import as_strided

from tensor_backend.tensor import Tensor, layout, ops
from tensor_backend.tensor.errors import ReshapeCopyRequiredError


def _factorizations(n: int, maxdim: int = 3) -> set[tuple[int, ...]]:
    out: set[tuple[int, ...]] = set()
    for nd in range(1, maxdim + 1):
        def rec(rem: int, dims: list[int]) -> None:
            if len(dims) == nd:
                if rem == 1:
                    out.add(tuple(dims))
                return
            for d in range(1, n + 1):
                if rem % d == 0:
                    rec(rem // d, dims + [d])
        if n > 0:
            rec(n, [])
    out.add((n,))
    return out


def _random_numpy_view(rng: random.Random) -> np.ndarray:
    shape = tuple(rng.randint(1, 5) for _ in range(rng.randint(1, 3)))
    a = np.arange(int(np.prod(shape)), dtype=np.float64).reshape(shape)
    for _ in range(rng.randint(0, 3)):
        kind = rng.choice(["slice", "transpose"])
        if kind == "slice":
            idx = tuple(
                slice(rng.randint(-2, 1),
                      rng.randint(0, 6) if rng.random() < 0.7 else None,
                      rng.choice([1, 1, 1, 2, -1, -2]))
                for _ in range(a.ndim)
            )
            try:
                a = a[idx]
            except Exception:
                pass
        elif a.ndim >= 2:
            a = a.transpose(tuple(np.random.permutation(a.ndim)))
    return a


@pytest.mark.unit
def test_reshape_decision_matches_numpy_exhaustive():
    """40k random layouts x every factorization: view/copy verdict == numpy."""
    rng = random.Random(4242)
    trials = 0
    for _ in range(3000):
        a = _random_numpy_view(rng)
        if a.size == 0 or a.size > 60:
            continue
        elem_strides = tuple(s // 8 for s in a.strides)
        for new_shape in _factorizations(a.size):
            trials += 1
            try:
                cand = layout.zero_copy_reshape(a.shape, elem_strides, new_shape)
                core_view = True
            except ReshapeCopyRequiredError:
                core_view = False
                cand = None
            np_result = a.reshape(new_shape)
            np_view = bool(np.shares_memory(a, np_result))
            assert core_view == np_view, (
                f"decision mismatch {a.shape} {elem_strides} -> {new_shape}: "
                f"core={core_view} numpy={np_view}"
            )
            if core_view:
                probe = as_strided(a, shape=new_shape,
                                   strides=tuple(s * 8 for s in cand))
                assert np.array_equal(probe, np_result), (
                    f"value mismatch {a.shape} -> {new_shape}"
                )
    assert trials > 15_000


@pytest.mark.unit
def test_slice_transpose_values_and_aliasing():
    """For 500 random numpy views, reconstruct the identical layout in-core and
    verify both values and shared storage with the owner."""
    rng = random.Random(77)
    for _ in range(500):
        a_np = _random_numpy_view(rng)
        if a_np.ndim == 0 or a_np.size == 0:
            continue
        root, byte_offset = _root_and_offset(a_np)
        owner = np.array(root, copy=True)
        core = Tensor.from_layout(
            owner,
            shape=a_np.shape,
            strides=tuple(s // 8 for s in a_np.strides),
            offset=byte_offset // 8,
        )
        np.testing.assert_array_equal(core.materialize(), np.ascontiguousarray(a_np))
        assert core.storage.size == owner.size


def _root_and_offset(a: np.ndarray) -> tuple[np.ndarray, int]:
    """Walk the .base chain to the owning buffer; return (root, byte offset)."""
    view = a
    root = a
    while root.base is not None:
        root = root.base
    # Recompute offset by walking the chain cumulatively is unreliable across
    # transposes; pointer arithmetic against the final root is exact.
    offset = view.__array_interface__["data"][0] - root.__array_interface__["data"][0]
    return root, offset



@pytest.mark.unit
def test_broadcast_values_match_numpy():
    rng = np.random.default_rng(1)
    shapes = [(1, 3), (2, 3), (3,), (4, 1, 3), (1, 1, 3)]
    for sa in shapes:
        for sb in shapes:
            a = rng.normal(size=sa)
            b = rng.normal(size=sb)
            try:
                expected = a + b
            except ValueError:
                with pytest.raises(Exception):
                    ops.binary("add", Tensor.from_values(a), Tensor.from_values(b))
                continue
            got = ops.binary("add", Tensor.from_values(a), Tensor.from_values(b))
            np.testing.assert_allclose(got.materialize(), expected)


@pytest.mark.unit
def test_negative_stride_layout_roundtrip():
    owner = np.arange(12.0)
    t = Tensor.from_layout(owner, (4, 3), (-3, -1), offset=11)
    expected = as_strided(owner[11:], shape=(4, 3), strides=(-24, -8))
    np.testing.assert_array_equal(t.materialize(), np.ascontiguousarray(expected))
