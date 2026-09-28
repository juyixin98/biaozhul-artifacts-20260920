"""Scaler state-machine tests: expected scales are hardcoded literals."""

from mptrainer.scaler import DynamicLossScaler


def test_backoff_on_overflow():
    s = DynamicLossScaler(scale=1024.0, growth_interval=3)
    assert s.update(overflow=True) == 512.0
    assert s.update(overflow=True) == 256.0
    assert s.good_steps == 0


def test_growth_after_interval_clean_commits():
    s = DynamicLossScaler(scale=100.0, growth_interval=3)
    assert s.update(overflow=False) == 100.0   # good_steps=1
    assert s.update(overflow=False) == 100.0   # good_steps=2
    assert s.update(overflow=False) == 200.0   # good_steps=3 -> grow, reset
    assert s.good_steps == 0


def test_overflow_resets_growth_progress():
    s = DynamicLossScaler(scale=100.0, growth_interval=2)
    s.update(overflow=False)                   # good_steps=1
    s.update(overflow=True)                    # scale=50, good_steps=0
    assert s.scale == 50.0
    assert s.update(overflow=False) == 50.0    # good_steps=1, no growth yet
    assert s.update(overflow=False) == 100.0   # good_steps=2 -> grow


def test_scale_never_drops_below_one():
    s = DynamicLossScaler(scale=1.0)
    assert s.update(overflow=True) == 1.0


def test_state_dict_roundtrip_preserves_machine():
    s = DynamicLossScaler(scale=256.0, growth_interval=5)
    s.update(overflow=False)
    s.update(overflow=True)
    clone = DynamicLossScaler.from_state_dict(s.state_dict())
    assert clone.scale == 128.0
    assert clone.good_steps == 0
    assert clone.growth_interval == 5
    # Same future behaviour, not just same fields:
    assert clone.update(overflow=False) == s.update(overflow=False)
