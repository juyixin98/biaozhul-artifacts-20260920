"""Unit tests for uneven-tail shard planning and re-shard index maps."""

from __future__ import annotations

import pytest

from adam_shard.sharding import ShardPlan, reshard_indices


def test_uneven_tail_lives_on_last_rank():
    plan = ShardPlan.create(59, 2)
    assert plan.sizes() == (29, 30)
    assert plan.boundaries == ((0, 29), (29, 59))


def test_three_way_split_is_also_uneven():
    plan = ShardPlan.create(59, 3)
    assert plan.sizes() == (19, 19, 21)
    assert sum(plan.sizes()) == 59


def test_plan_rejects_world_larger_than_numel():
    with pytest.raises(ValueError):
        ShardPlan.create(2, 3)


@pytest.mark.parametrize("total,world", [(59, 2), (59, 3), (60, 4), (1, 1), (7, 5)])
def test_plan_full_coverage_no_overlap(total, world):
    plan = ShardPlan.create(total, world)
    spans = plan.boundaries
    assert spans[0][0] == 0 and spans[-1][1] == total
    for (_, e), (s2, _) in zip(spans, spans[1:]):
        assert e == s2


def test_reshard_2_to_3_covers_every_element_once():
    src = ShardPlan.create(59, 2)
    dst = ShardPlan.create(59, 3)
    segments = reshard_indices(src, dst)

    covered_global: set[int] = set()
    for d_rank, segs in enumerate(segments):
        d_start, _ = dst.span(d_rank)
        for s_rank, s_off, d_off, length in segs:
            s_start, _ = src.span(s_rank)
            for i in range(length):
                covered_global.add(s_start + s_off + i)
                # local destination index maps to the same global index
                assert s_start + s_off + i == d_start + d_off + i
    assert covered_global == set(range(59))


def test_reshard_3_to_2_roundtrip_partition():
    src = ShardPlan.create(59, 3)  # [0,19) [19,38) [38,59)
    dst = ShardPlan.create(59, 2)  # [0,29) [29,59) -- tail on last rank
    segments = reshard_indices(src, dst)
    # (src_rank, src_offset, dst_offset, length)
    assert segments[0] == [(0, 0, 0, 19), (1, 0, 19, 10)]
    assert segments[1] == [(1, 10, 0, 9), (2, 0, 9, 21)]


def test_reshard_rejects_different_totals():
    with pytest.raises(ValueError):
        reshard_indices(ShardPlan.create(10, 2), ShardPlan.create(11, 2))
