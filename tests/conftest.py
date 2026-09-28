"""测试公共夹具与图构建器。

所有图都以"原始字典"（API 入口形态）构造，确保测试覆盖真实输入路径。
参考答案的来源在各自测试中注明：手算冻结值 / 独立有限差分 / 引擎运行时高水位，
参考答案不由 memory 模拟器（被测核心）单独生成。
"""

from __future__ import annotations

import pytest

from recomp_scheduler.graph import build_graph
from recomp_scheduler.journal import Journal
from recomp_scheduler.state import TrainState


def linear_chain_raw() -> list[dict]:
    """x(4x2) -> linear(3) -> relu -> dropout(0.5) -> linear(1)。"""
    return [
        {"id": "x", "op": "input", "attrs": {"shape": [4, 2]}},
        {"id": "l1", "op": "linear", "inputs": ["x"], "attrs": {"out_features": 3}},
        {"id": "r1", "op": "relu", "inputs": ["l1"]},
        {"id": "d1", "op": "dropout", "inputs": ["r1"], "attrs": {"keep_prob": 0.5}},
        {"id": "l2", "op": "linear", "inputs": ["d1"], "attrs": {"out_features": 1}},
    ]


def branch_raw() -> list[dict]:
    """d1 被两个分支 a1/b1 共享后 add 合并。"""
    return [
        {"id": "x", "op": "input", "attrs": {"shape": [3, 4]}},
        {"id": "l1", "op": "linear", "inputs": ["x"], "attrs": {"out_features": 5}},
        {"id": "r1", "op": "relu", "inputs": ["l1"]},
        {"id": "d1", "op": "dropout", "inputs": ["r1"], "attrs": {"keep_prob": 0.6}},
        {"id": "a1", "op": "tanh", "inputs": ["d1"]},
        {"id": "b1", "op": "relu", "inputs": ["d1"]},
        {"id": "m", "op": "add", "inputs": ["a1", "b1"]},
        {"id": "l2", "op": "linear", "inputs": ["m"], "attrs": {"out_features": 2}},
    ]


def cross_block_shared_raw() -> list[dict]:
    """r1 被 a 链与延迟分支 b1 跨块共享；d1 在检查点块内部（需重放）。"""
    return [
        {"id": "x", "op": "input", "attrs": {"shape": [3, 4]}},
        {"id": "l1", "op": "linear", "inputs": ["x"], "attrs": {"out_features": 5}},
        {"id": "r1", "op": "relu", "inputs": ["l1"]},
        {"id": "d1", "op": "dropout", "inputs": ["r1"], "attrs": {"keep_prob": 0.6}},
        {"id": "a1", "op": "tanh", "inputs": ["d1"]},
        {"id": "a2", "op": "relu", "inputs": ["a1"]},
        {"id": "b1", "op": "tanh", "inputs": ["r1"]},
        {"id": "m", "op": "add", "inputs": ["a2", "b1"]},
        {"id": "l2", "op": "linear", "inputs": ["m"], "attrs": {"out_features": 2}},
    ]


def big_chain_raw(n_mid: int = 6, width: int = 100) -> list[dict]:
    """大激活长链：用于展示检查点显著降低峰值。"""
    nodes: list[dict] = [
        {"id": "x", "op": "input", "attrs": {"shape": [8, width]}}
    ]
    prev = "x"
    ops = ["relu", "tanh", "relu", "dropout", "tanh", "relu"]
    for i, op in enumerate(ops[:n_mid]):
        nid = f"h{i}"
        attrs = {"keep_prob": 0.7} if op == "dropout" else {}
        nodes.append({"id": nid, "op": op, "inputs": [prev], "attrs": attrs})
        prev = nid
    nodes.append(
        {"id": "y", "op": "linear", "inputs": [prev], "attrs": {"out_features": 2}}
    )
    return nodes


def tiny_linear_raw() -> list[dict]:
    """x(2x3) -> linear(4) -> linear(1)：峰值 78 已手算冻结。"""
    return [
        {"id": "x", "op": "input", "attrs": {"shape": [2, 3]}},
        {"id": "h1", "op": "linear", "inputs": ["x"], "attrs": {"out_features": 4}},
        {"id": "y", "op": "linear", "inputs": ["h1"], "attrs": {"out_features": 1}},
    ]


@pytest.fixture
def chain_graph():
    return build_graph(linear_chain_raw(), outputs=["l2"])


@pytest.fixture
def branch_graph():
    return build_graph(branch_raw(), outputs=["l2"])


@pytest.fixture
def cross_graph():
    return build_graph(cross_block_shared_raw(), outputs=["l2"])


@pytest.fixture
def big_graph():
    return build_graph(big_chain_raw(), outputs=["y"])


@pytest.fixture
def tiny_graph():
    return build_graph(tiny_linear_raw(), outputs=["y"])


def make_state(graph, seed: int = 99) -> TrainState:
    return TrainState.synthetic(graph, seed=seed)


@pytest.fixture
def tmp_journal(tmp_path) -> Journal:
    return Journal(str(tmp_path / "logs"))


def deterministic_seed() -> int:
    """固定种子（测试可复现）。"""
    return 20260928
