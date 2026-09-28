"""LR schedule: progress is driven only by committed steps."""

import pytest

from amptrain.config import OptimizerConfig
from amptrain.scheduler import LRScheduler


def test_constant_schedule_never_changes():
    sched = LRScheduler(OptimizerConfig(schedule="constant", lr=0.05))
    lrs = [sched.learning_rate()]
    for _ in range(20):
        sched = sched.advanced()
        lrs.append(sched.learning_rate())
    assert all(lr == 0.05 for lr in lrs)


def test_step_schedule_drops_every_step_size_committed_steps():
    sched = LRScheduler(
        OptimizerConfig(schedule="step", lr=0.1, step_size=5, gamma=0.5)
    )
    seen = []
    for committed in range(12):
        # learning_rate at `committed` committed steps
        assert sched.committed_steps == committed
        seen.append(sched.learning_rate())
        sched = sched.advanced()
    assert seen == [0.1, 0.1, 0.1, 0.1, 0.1, 0.05, 0.05, 0.05, 0.05, 0.05, 0.025, 0.025]


def test_scheduler_state_roundtrip():
    sched = LRScheduler(OptimizerConfig(schedule="step", lr=0.2, step_size=2, gamma=0.1))
    sched = sched.advanced().advanced().advanced()  # 3 committed -> one drop
    restored = LRScheduler.from_state(sched.cfg, sched.to_state())
    assert restored.committed_steps == 3
    assert restored.learning_rate() == pytest.approx(sched.learning_rate())
    assert restored.learning_rate() == pytest.approx(0.02)
