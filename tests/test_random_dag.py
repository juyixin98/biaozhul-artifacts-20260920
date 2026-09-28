"""随机分支 DAG 属性测试。

用确定性种子生成若干"主干 + 随机跳跃合并"的小 DAG（天然无环、全部节点可达
输出、含随机 fanout 即共享子图），在每张图的**全部合法检查点方案**上断言：

1. 独立区间模型 == 生产模拟器 == 引擎运行时高水位；
2. 检查点方案的梯度/输出与不检查点基线逐元素相等。

这是在固定夹具之外的、跨多种图结构的交叉验证。
"""

from __future__ import annotations

import numpy as np
import pytest

from recomp_scheduler.engine import execute
from recomp_scheduler.graph import build_graph
from recomp_scheduler.memory import simulate
from recomp_scheduler.planner import baseline_plan, legal_plans
from recomp_scheduler.state import TrainState

from .independent_model import independent_peak

_UNARY = ["relu", "tanh", "dropout", "linear", "tanh", "relu"]
_MERGE = ["add", "mul"]


def _random_dag(seed: int, n_nodes: int = 8, width: int = 3, batch: int = 2):
    """生成主干型随机 DAG 的原始节点声明（拓扑序、全部可达最后一个节点）。"""
    rng = np.random.default_rng(seed)
    nodes = [
        {"id": "x", "op": "input", "attrs": {"shape": [batch, width]}}
    ]
    prev = "x"
    spine = ["x"]
    for i in range(n_nodes - 1):
        nid = f"n{i}"
        # 一定以直接前驱为输入，保证所有节点都在通向最终输出的主干上。
        roll = rng.random()
        if i >= 2 and roll < 0.3:
            # 二元合并：前驱 + 随机更早的同形节点（产生 fanout/共享子图）。
            other = spine[int(rng.integers(0, len(spine) - 1))]
            op = str(rng.choice(_MERGE))
            nodes.append({"id": nid, "op": op, "inputs": [prev, other]})
        else:
            op = str(rng.choice(_UNARY))
            attrs: dict = {}
            if op == "linear":
                attrs = {"out_features": width}
            elif op == "dropout":
                attrs = {"keep_prob": float(rng.choice([0.5, 0.7, 1.0]))}
            nodes.append({"id": nid, "op": op, "inputs": [prev], "attrs": attrs})
        prev = nid
        spine.append(nid)
    # 保证最后一个节点是可微的非输入计算节点且形状明确。
    return nodes, prev


@pytest.mark.integration
@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5, 6])
def test_random_dags_three_way_and_gradient(seed: int) -> None:
    raw, out_id = _random_dag(seed)
    graph = build_graph(raw, outputs=[out_id])

    base_plan, base_prof = baseline_plan(graph)
    baseline = execute(
        TrainState.synthetic(graph, seed=1000 + seed),
        base_plan,
        predicted_peak=base_prof.peak_elements,
    )

    scored = legal_plans(graph)
    assert len(scored) >= 1
    for plan, profile in scored:
        # 1) 三方内存对账
        assert independent_peak(graph, plan) == profile.peak_elements
        result = execute(
            TrainState.synthetic(graph, seed=1000 + seed),
            plan,
            predicted_peak=profile.peak_elements,
        )
        assert result.runtime_peak == profile.peak_elements

        # 2) 梯度/输出与基线逐元素相等（RNG 重放必须精确）
        np.testing.assert_array_equal(result.outputs[out_id], baseline.outputs[out_id])
        for nid in baseline.param_grads:
            for p in baseline.param_grads[nid]:
                np.testing.assert_array_equal(
                    result.param_grads[nid][p], baseline.param_grads[nid][p]
                )
        for v in baseline.input_grads:
            np.testing.assert_array_equal(
                result.input_grads[v], baseline.input_grads[v]
            )
