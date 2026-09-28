"""Layer 2: aggregation and clipping primitives.

Expected numbers come from the hand-authored fixture and closed-form hand
calculations, not from the implementation.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

from sparse_embedding.config import ClipConfig, ClipMode
from sparse_embedding.errors import EmptyBatchError
from sparse_embedding.graph import aggregate_duplicates, clip_gradients
from sparse_embedding.tensors import SparseGradientBatch
from sparse_embedding.config import TableSpec

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SPEC = TableSpec(num_rows=6, dim=2)


def load_batches():
    with open(FIXTURES / "batches_small.json", encoding="utf-8") as fh:
        return {b["id"]: b for b in json.load(fh)["batches"]}


BATCHES = load_batches()


def agg_for(batch_id: str):
    b = BATCHES[batch_id]
    sg = SparseGradientBatch.from_pairs(b["indices"], b["values"], SPEC)
    return aggregate_duplicates(sg)


def test_duplicate_indices_sum_not_overwrite():
    # The critical semantics: np.add.at accumulates repeated indices.
    a = agg_for("duplicates_basic")
    assert a.unique_indices.tolist() == [0, 2]
    np.testing.assert_allclose(
        a.aggregated, [[4, 4], [0, 3]], atol=1e-12
    )


def test_aggregation_matches_hand_authored_fixture():
    a = agg_for("duplicates_basic")
    expected = BATCHES["duplicates_basic"]
    np.testing.assert_allclose(a.aggregated, expected["expected_aggregated"], atol=1e-12)
    np.testing.assert_allclose(a.row_norms, expected["expected_row_norms"], atol=1e-12)
    assert math.isclose(a.global_norm, expected["expected_global_norm"], rel_tol=1e-12)
    assert a.raw_count == 5
    assert a.touched_count == 2


def test_zero_aggregated_row_is_present_after_aggregation():
    # Aggregation keeps a row that sums to zero; the zero-step rule belongs to
    # the training layer, not aggregation.
    a = agg_for("hot_cold_zero_row")
    assert a.unique_indices.tolist() == [0, 1, 5]
    np.testing.assert_allclose(a.aggregated[a.unique_indices.tolist().index(5)], [0, 0])


def test_aggregate_empty_batch_is_explicit_error():
    sg = SparseGradientBatch.from_pairs([], [], SPEC)
    with pytest.raises(EmptyBatchError):
        aggregate_duplicates(sg)


# --------------------------------------------------------------------- clip


def test_no_clip_is_identity_and_reports_mode():
    a = agg_for("duplicates_basic")
    out, report = clip_gradients(a, ClipConfig(ClipMode.NONE))
    np.testing.assert_allclose(out, a.aggregated)
    assert report["mode"] == "none"
    assert report["scale"] == 1.0


def test_global_clipping_uses_one_shared_scale():
    a = agg_for("duplicates_basic")
    max_norm = 4.0
    out, report = clip_gradients(a, ClipConfig(ClipMode.GLOBAL, max_norm))
    expected_scale = max_norm / math.sqrt(41)  # global norm = sqrt(41)
    assert report["applied"] is True
    assert math.isclose(report["scale"], expected_scale, rel_tol=1e-12)
    # One uniform factor applied to BOTH rows -> proves it is not per-row.
    np.testing.assert_allclose(out, a.aggregated * expected_scale)
    assert math.isclose(np.linalg.norm(out), max_norm, rel_tol=1e-12)


def test_global_clipping_noop_when_under_threshold():
    a = agg_for("duplicates_basic")
    out, report = clip_gradients(a, ClipConfig(ClipMode.GLOBAL, 100.0))
    assert report["applied"] is False
    assert report["scale"] == 1.0
    np.testing.assert_allclose(out, a.aggregated)


def test_row_clipping_uses_independent_per_row_scales():
    a = agg_for("duplicates_basic")
    max_norm = 2.0
    out, report = clip_gradients(a, ClipConfig(ClipMode.ROW, max_norm))
    # Row0 norm sqrt(32) ~5.66 -> clipped to 2. Row1 norm 3 -> clipped to 2.
    # Factors differ, which is the whole point vs global clipping.
    expected_scales = [max_norm / math.sqrt(32), max_norm / 3.0]
    np.testing.assert_allclose(report["scales"], expected_scales, atol=1e-12)
    post_norms = np.linalg.norm(out, axis=1)
    np.testing.assert_allclose(post_norms, [2.0, 2.0], atol=1e-12)


def test_row_clipping_leaves_small_rows_untouched():
    # Rows under the per-row threshold keep scale exactly 1.
    a = agg_for("hot_cold_zero_row")
    out, report = clip_gradients(a, ClipConfig(ClipMode.ROW, 5.0))
    # norms: row0=sqrt2 (~1.41), row1=sqrt8 (~2.83), row5=0
    np.testing.assert_allclose(report["scales"], [1.0, 1.0, 1.0])
    np.testing.assert_allclose(out, a.aggregated)


def test_global_and_row_modes_cannot_be_mixed():
    # A global clip must NOT equal a row clip when row norms straddle the
    # threshold: the two code paths give provably different answers.
    a = agg_for("duplicates_basic")
    g_out, _ = clip_gradients(a, ClipConfig(ClipMode.GLOBAL, 4.0))
    r_out, _ = clip_gradients(a, ClipConfig(ClipMode.ROW, 4.0))
    assert not np.allclose(g_out, r_out), "global and row clipping were mixed!"


def test_clipping_does_not_mutate_input():
    a = agg_for("duplicates_basic")
    original = a.aggregated.copy()
    clip_gradients(a, ClipConfig(ClipMode.GLOBAL, 1.0))
    np.testing.assert_array_equal(a.aggregated, original)


def test_zero_norm_row_under_row_clipping_is_safe():
    # Pure zero row: no 0/0, scale stays 1, output stays zero.
    a = agg_for("hot_cold_zero_row")
    out, report = clip_gradients(a, ClipConfig(ClipMode.ROW, 1.0))
    row5_pos = a.unique_indices.tolist().index(5)
    assert report["scales"][row5_pos] == 1.0
    np.testing.assert_array_equal(out[row5_pos], [0.0, 0.0])
