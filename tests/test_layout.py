"""Unit tests for flatten/unflatten with reordered dict traversal."""

from __future__ import annotations

import numpy as np

from adam_shard.layout import ParamLayout


def test_flatten_is_traversal_order_independent(spec):
    layout = ParamLayout(tuple(spec.parameter_ids()))
    rng = np.random.default_rng(0)
    params = {tid.name: rng.standard_normal(tid.shape) for tid in layout.ids}

    forward = dict(list(params.items()))
    reversed_order = dict(list(params.items())[::-1])
    shuffled = dict(sorted(params.items(), key=lambda kv: (len(kv[0]), kv[0])))

    flat_a = layout.flatten(forward)
    assert np.array_equal(flat_a, layout.flatten(reversed_order))
    assert np.array_equal(flat_a, layout.flatten(shuffled))


def test_roundtrip_preserves_named_shapes(spec):
    layout = ParamLayout(tuple(spec.parameter_ids()))
    params = {tid.name: np.arange(tid.numel, dtype=np.float64).reshape(tid.shape) for tid in layout.ids}
    restored = layout.unflatten(layout.flatten(params))
    assert set(restored) == set(params)
    for name, arr in params.items():
        assert restored[name].shape == arr.shape
        assert np.array_equal(restored[name], arr)


def test_manifest_offsets_are_contiguous_and_sorted(spec):
    layout = ParamLayout(tuple(spec.parameter_ids()))
    records = layout.to_manifest()
    assert records[0]["offset"] == 0
    for prev, cur in zip(records, records[1:]):
        assert prev["offset"] + prev["numel"] == cur["offset"]
    assert [r["name"] for r in records] == sorted(r["name"] for r in records)


def test_flatten_rejects_wrong_numel(spec):
    layout = ParamLayout(tuple(spec.parameter_ids()))
    bad = {tid.name: np.zeros(tid.numel + 1) for tid in layout.ids}
    try:
        layout.flatten(bad)
    except ValueError:
        return
    raise AssertionError("expected ValueError for mismatched numel")


def test_manifest_rejects_gap():
    records = [
        {"name": "a", "shape": [2], "offset": 0, "numel": 2},
        {"name": "b", "shape": [2], "offset": 3, "numel": 2},
    ]
    try:
        ParamLayout.from_manifest(records)
    except ValueError:
        return
    raise AssertionError("expected gap rejection")
