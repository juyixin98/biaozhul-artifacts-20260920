"""Arena 记账、引用计数、工作区与运行时预算测试。"""

from __future__ import annotations

import numpy as np
import pytest

from recomp_scheduler.errors import BudgetInfeasibleError, StateConflictError
from recomp_scheduler.tensor import Arena, require_finite


def test_allocate_release_and_peak() -> None:
    arena = Arena()
    a = arena.allocate("a", np.zeros((3, 4)))
    b = arena.allocate("b", np.zeros((2, 2)))
    assert arena.live_elements == 16
    arena.release(a)
    assert arena.live_elements == 4
    # 峰值是历史高水位，不随释放下降。
    assert arena.peak_elements == 16
    arena.release(b)
    assert arena.close()["peak_elements"] == 16


def test_workspace_counts_toward_peak() -> None:
    arena = Arena()
    a = arena.allocate("a", np.zeros(10))
    assert arena.peak_live_only == 10
    with arena.enter_workspace(7, "tmp"):
        assert arena.workspace_elements == 7
        assert arena.peak_elements == 17  # 10 存活 + 7 临时工作区
    assert arena.workspace_elements == 0
    arena.release(a)
    arena.close()


def test_duplicate_name_is_state_conflict() -> None:
    arena = Arena()
    arena.allocate("t", np.zeros(3))
    with pytest.raises(StateConflictError) as exc:
        arena.allocate("t", np.zeros(3))
    assert exc.value.category == "state_conflict"


def test_double_free_is_state_conflict() -> None:
    arena = Arena()
    t = arena.allocate("t", np.zeros(2))
    arena.release(t)
    with pytest.raises(StateConflictError):
        arena.release(t)


def test_close_with_leak_is_state_conflict() -> None:
    arena = Arena()
    arena.allocate("leak", np.zeros(5))
    with pytest.raises(StateConflictError) as exc:
        arena.close()
    assert exc.value.details["tensors"] == ["leak"]


def test_unclosed_workspace_is_state_conflict() -> None:
    arena = Arena()
    arena.enter_workspace(4, "forgot-to-exit")
    t = arena.allocate("t", np.zeros(1))
    arena.release(t)
    with pytest.raises(StateConflictError) as exc:
        arena.close()
    assert exc.value.details["open_workspaces"] == 1


def test_shared_reference_counting() -> None:
    arena = Arena()
    shared = arena.allocate("s", np.zeros(6))
    # 模拟共享子图输出被 3 个消费者持有：初始 1 + 2 次 retain。
    arena.retain(shared, holder="c1")
    arena.retain(shared, holder="c2")
    assert shared.ref_count == 3
    arena.release(shared)  # c1 用完
    arena.release(shared)  # c2 用完
    assert not shared.freed  # 原始持有者仍在
    assert arena.live_elements == 6
    arena.release(shared)  # 最后一个持有者
    assert shared.freed
    assert arena.live_elements == 0
    arena.close()


def test_runtime_budget_enforced_hard() -> None:
    arena = Arena(budget_elements=10)
    arena.allocate("ok", np.zeros(10))
    with pytest.raises(BudgetInfeasibleError) as exc:
        arena.allocate("overflow", np.zeros(1))
    err = exc.value
    assert err.category == "resource_exhausted"
    assert err.details["phase"] == "runtime"
    assert err.details["budget"] == 10
    assert err.details["over_by"] == 1


def test_runtime_budget_covers_workspace() -> None:
    arena = Arena(budget_elements=12)
    arena.allocate("a", np.zeros(10))
    with pytest.raises(BudgetInfeasibleError):
        with arena.enter_workspace(5, "tmp"):
            pass


def test_finite_guard_detects_non_finite() -> None:
    from recomp_scheduler.errors import ComputeFailureError

    arena = Arena()
    bad = arena.allocate("bad", np.array([1.0, np.nan, 3.0]))
    with pytest.raises(ComputeFailureError) as exc:
        require_finite(bad, "test-phase")
    assert exc.value.category == "compute_failure"
    assert exc.value.details["bad_elements"] == 1
    arena.release(bad)
    arena.close()
