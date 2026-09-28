"""Tests for the fixed bucket layout and explicit placeholder handling."""

import numpy as np
import pytest

from bucket_sync.bucketing import BucketLayout, plan_buckets
from bucket_sync.config import make_graph_with_frozen_bias

pytestmark = pytest.mark.unit


def test_layout_covers_flat_vector_without_gaps_or_overlap(layout):
    covered = np.zeros(layout.flat_size, dtype=bool)
    for bucket in layout.buckets:
        assert bucket.end > bucket.start
        covered[bucket.start : bucket.end] = True
    assert covered.all()
    assert layout.flat_size == 4  # w(3) + b(1) for 3 features


def test_layout_is_identical_regardless_of_construction_order(graph, cfg):
    layout_a = BucketLayout(graph, cfg.bucket_size)
    layout_b = plan_buckets(graph, cfg.bucket_size)
    bounds_a = [(b.start, b.end) for b in layout_a.buckets]
    bounds_b = [(b.start, b.end) for b in layout_b.buckets]
    assert bounds_a == bounds_b
    assert layout_a.fingerprint() == layout_b.fingerprint()


def test_frozen_parameter_keeps_its_slot_as_explicit_placeholder(cfg):
    graph = make_graph_with_frozen_bias(cfg.in_features)
    layout = BucketLayout(graph, cfg.bucket_size)
    # same flat size as the all-trainable graph: the bias slot is retained
    assert layout.flat_size == cfg.in_features + 1
    mask = layout.placeholder_mask()
    w_slice = layout.slice_for("w")
    b_slice = layout.slice_for("b")
    assert mask[w_slice.offset : w_slice.offset + w_slice.size].any() == np.bool_(False)
    assert mask[b_slice.offset : b_slice.offset + b_slice.size].all()


def test_pack_unpack_roundtrips_and_missing_param_becomes_zero_placeholder(layout):
    rng = np.random.default_rng(0)
    w = rng.normal(size=(3, 1))
    flat = layout.pack_flat({"w": w})  # bias intentionally absent
    assert flat[-1] == 0.0  # explicit placeholder zero in a reserved slot
    out = layout.pack_flat({"w": w, "b": np.array([0.5])})
    assert out[-1] == pytest.approx(0.5)
    unpacked = layout.unpack_flat(out)
    np.testing.assert_allclose(unpacked["w"], w)
    assert unpacked["b"].shape == (1,)


def test_pack_rejects_unknown_parameter_instead_of_silently_dropping(layout):
    with pytest.raises(Exception, match="unknown parameters"):
        layout.pack_flat({"w": np.zeros((3, 1)), "typo": np.zeros(1)})


def test_pack_rejects_wrong_sized_value(layout):
    with pytest.raises(Exception):
        layout.pack_flat({"w": np.zeros((2, 1)), "b": np.zeros(1)})


def test_bucket_bounds_never_cross_parameter_slot_inconsistency(graph, cfg):
    # Every parameter slice must be reconstructable from the flat vector.
    layout = BucketLayout(graph, cfg.bucket_size)
    for node in graph:
        sl = layout.slice_for(node.spec.name)
        assert sl.size == node.spec.size
        assert 0 <= sl.offset < layout.flat_size
        assert sl.offset + sl.size <= layout.flat_size


def test_invalid_bucket_size_rejected(graph):
    with pytest.raises(Exception):
        BucketLayout(graph, 0)
