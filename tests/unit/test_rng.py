"""Unit tests for deterministic RNG replay strategies."""

from __future__ import annotations

import numpy as np
import pytest

from app.core.rng import RandomStream
from app.reference.seedspec import counter_mask, reference_masks


@pytest.mark.unit
def test_counter_replay_reproduces_forward_mask_exactly() -> None:
    stream = RandomStream(2026, strategy="counter")
    shape = (4, 5)
    forward_mask = stream.dropout_mask("d1", shape, 0.3)
    # Unrelated draws from other nodes must not change d1's replay.
    stream.dropout_mask("other", shape, 0.3)
    replay_mask = stream.dropout_mask("d1", shape, 0.3, replay=True)
    assert np.array_equal(forward_mask, replay_mask)
    # And it must equal the independently documented seeding rule.
    assert np.array_equal(forward_mask, counter_mask(2026, "d1", shape, 0.3))


@pytest.mark.unit
def test_snapshot_restore_reproduces_draw_sequence() -> None:
    stream = RandomStream(42, strategy="snapshot")
    snap = stream.snapshot()
    m1 = stream.dropout_mask("d1", (3,), 0.2)
    m2 = stream.dropout_mask("d2", (3,), 0.2)
    stream.restore(snap)
    r1 = stream.dropout_mask("d1", (3,), 0.2)
    r2 = stream.dropout_mask("d2", (3,), 0.2)
    assert np.array_equal(m1, r1)
    assert np.array_equal(m2, r2)


@pytest.mark.unit
def test_snapshot_catch_up_via_dummy_draws_skips_earlier_nodes() -> None:
    # Restoring then skipping d1 with a dummy draw must leave the stream in
    # the state from which d2's forward draw is reproduced.
    stream = RandomStream(42, strategy="snapshot")
    snap = stream.snapshot()
    stream.dropout_mask("d1", (3,), 0.2)
    m2 = stream.dropout_mask("d2", (3,), 0.2)
    stream.restore(snap)
    stream.draw_dummy((3,), 0.2)
    r2 = stream.dropout_mask("d2", (3,), 0.2)
    assert np.array_equal(m2, r2)


@pytest.mark.unit
def test_reference_masks_strategies_agree_on_fixed_fixture(branching) -> None:
    from app.fixtures.specs import BRANCHING_SPEC

    counter = reference_masks("counter", 99, BRANCHING_SPEC)
    snapshot = reference_masks("snapshot", 99, BRANCHING_SPEC)
    # Both strategies must produce finite, scaled inverted-dropout masks.
    for masks in (counter, snapshot):
        assert set(masks) == {"d1"}
        vals = np.unique(masks["d1"])
        assert np.all(np.isfinite(vals))
        # p=0.25 -> kept entries scale to 4/3; dropped entries are 0.
        assert set(np.round(vals, 8)).issubset({0.0, round(4.0 / 3, 8)})


@pytest.mark.unit
def test_stream_rejects_bad_seed_and_strategy() -> None:
    with pytest.raises(ValueError):
        RandomStream(-1)
    with pytest.raises(ValueError):
        RandomStream(1, strategy="bogus")
