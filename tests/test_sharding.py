"""Unit tests for layout, uneven tails and cross-world-size round trips."""
from __future__ import annotations

import numpy as np
import pytest

from adam_shards.sharding import (
    FlatLayout,
    plan_ranges,
    restore_arrays,
    save_checkpoint,
    slices_for_range,
)


def test_plan_ranges_covers_total_with_uneven_tail():
    # 9 elements over 2 ranks -> 5 and 4 (first ranks absorb remainder).
    assert plan_ranges(9, 2) == [(0, 5), (5, 9)]
    # 8 over 3 -> 3,3,2: the tail is uneven.
    assert plan_ranges(8, 3) == [(0, 3), (3, 6), (6, 8)]
    for total in range(1, 20):
        for ws in range(1, total + 1):
            ranges = plan_ranges(total, ws)
            assert ranges[0][0] == 0
            assert ranges[-1][1] == total
            assert all(ranges[i][1] == ranges[i + 1][0]
                       for i in range(len(ranges) - 1))
            # No rank is ever empty.
            assert all(hi > lo for lo, hi in ranges)


def test_slice_plan_straddles_parameter_boundary():
    layout = FlatLayout.build({"alpha": (5,), "beta": (2, 2)})  # 5 + 4
    first = slices_for_range(layout, 0, 5)
    second = slices_for_range(layout, 5, 9)
    # Boundary at flat index 5 is exactly the alpha/beta border here.
    assert [s.name for s in first] == ["alpha"]
    assert [s.name for s in second] == ["beta"]
    # Boundary at 3 cuts through alpha.
    cut = slices_for_range(layout, 0, 3) + slices_for_range(layout, 3, 9)
    names = [(s.name, s.start, s.end) for s in cut]
    assert ("alpha", 0, 3) in names and ("alpha", 3, 5) in names


@pytest.mark.parametrize("save_ws,restore_ws", [
    (1, 1), (2, 1), (2, 3), (3, 2), (2, 5), (5, 2), (9, 3), (3, 9),
])
def test_roundtrip_across_process_counts(small_state, tmp_path, save_ws, restore_ws):
    params, moments = small_state  # 9 flat elements
    src = tmp_path / f"save{save_ws}"
    save_checkpoint(str(src), params, moments, world_size=save_ws)
    loaded = restore_arrays(str(src), world_size=restore_ws)
    assert loaded["source_world_size"] == save_ws
    for name in params:
        np.testing.assert_array_equal(loaded["params"][name], params[name])
        m, v, step = moments[name]
        lm, lv, lstep = loaded["moments"][name]
        np.testing.assert_array_equal(lm, m)
        np.testing.assert_array_equal(lv, v)
        assert lstep == step


def test_restore_is_independent_of_parameter_traversal_order(saved_checkpoint):
    ckpt, _, params, moments = saved_checkpoint
    shapes_in_order = {n: params[n].shape for n in params}
    scrambled = dict(reversed(list(shapes_in_order.items())))
    loaded = restore_arrays(ckpt, world_size=3, target_shapes=scrambled)
    # Values still keyed by stable name, never by position.
    for name in params:
        np.testing.assert_array_equal(loaded["params"][name], params[name])
        assert loaded["moments"][name][2] == moments[name][2]
