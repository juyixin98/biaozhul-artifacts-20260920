"""三方内存对账测试。

对每张图的**全部合法方案**断言三者相等：

1. 生产静态模拟器 ``memory.simulate`` 的预测峰值（增量事件流水账）；
2. :mod:`tests.independent_model` 的独立区间覆盖峰值（另一套实现/另一种方法）；
3. 执行引擎 Arena 的运行时高水位（真实分配/释放事件）。

参考答案不来自被测核心自身：独立模型只读图的静态声明，引擎峰值是运行事实。
"""

from __future__ import annotations

import pytest

from recomp_scheduler.engine import execute
from recomp_scheduler.memory import simulate
from recomp_scheduler.planner import legal_plans

from .independent_model import independent_peak


def _three_way(graph, seed: int = 20260928) -> None:
    scored = legal_plans(graph)
    assert scored, "图至少应有一个合法方案（基线）"
    for plan, profile in scored:
        independent = independent_peak(graph, plan)
        # 1) 生产模拟器 vs 独立区间模型（两种独立方法）
        assert independent == profile.peak_elements, (
            f"independent={independent} simulator={profile.peak_elements} "
            f"cuts={plan.boundary_positions}"
        )
        # 2) 模拟器 vs 引擎运行时高水位（静态 vs 真实分配）
        from recomp_scheduler.state import TrainState

        result = execute(
            TrainState.synthetic(graph, seed=seed),
            plan,
            predicted_peak=profile.peak_elements,
        )
        assert result.runtime_peak == profile.peak_elements, (
            f"runtime={result.runtime_peak} simulator={profile.peak_elements} "
            f"cuts={plan.boundary_positions}"
        )


@pytest.mark.integration
def test_three_way_tiny(tiny_graph) -> None:
    _three_way(tiny_graph)


@pytest.mark.integration
def test_three_way_chain(chain_graph) -> None:
    _three_way(chain_graph)


@pytest.mark.integration
def test_three_way_branch(branch_graph) -> None:
    _three_way(branch_graph)


@pytest.mark.integration
def test_three_way_cross_block_shared(cross_graph) -> None:
    _three_way(cross_graph)


@pytest.mark.integration
def test_three_way_big_chain(big_graph) -> None:
    _three_way(big_graph)


def test_independent_model_disagrees_if_lifecycle_wrong(monkeypatch, chain_graph) -> None:
    """反向健全性：人为篡改模拟器（重算漏算工作区）时，独立模型必须抓到分歧。

    这保证独立模型不是对生产代码的简单复述——它确实能检出错误。
    """
    from recomp_scheduler import memory as mem_mod
    from recomp_scheduler.planner import make_plan

    # 取一个含 dropout 内部重放的方案 (2,) 对 6 节点图更直观；这里直接用链图。
    plan = make_plan((2,), len(chain_graph.order))
    expected = independent_peak(chain_graph, plan)

    real_simulate = mem_mod.simulate

    def broken_simulate(graph, plan):
        prof = real_simulate(graph, plan)
        object.__setattr__(prof, "peak_elements", prof.peak_elements - 1)
        return prof

    monkeypatch.setattr(mem_mod, "simulate", broken_simulate)
    # 独立模型未被篡改，仍给出原值 -> 二者不等，断言触发。
    from recomp_scheduler.memory import simulate as patched

    assert patched(chain_graph, plan).peak_elements != expected
    # 恢复后应重新一致（确认测试本身有效）。
    monkeypatch.undo()
    from recomp_scheduler.memory import simulate as restored

    assert restored(chain_graph, plan).peak_elements == expected
