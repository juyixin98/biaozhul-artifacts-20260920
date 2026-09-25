"""交叉验证：小图上标签算法 vs 全量简单路径枚举。"""

import itertools
import random

import pytest

from mospp.graph import Graph
from mospp.solver import Solver
from mospp.tolerance import DEFAULT_EPS

from .reference import reference_pareto, fronts_equal_with_tol


def random_dag(rng, n, p, max_w=5):
    """随机 DAG（保证简单路径数量可控）。"""
    edges = []
    for u in range(n):
        for v in range(u + 1, n):
            if rng.random() < p:
                edges.append((u, v, rng.randint(0, max_w), rng.randint(0, max_w)))
    return edges


def random_graph(rng, n, p, max_w=4):
    """随机一般有向图（可能含环，含零权边）。"""
    edges = []
    for u, v in itertools.product(range(n), range(n)):
        if u != v and rng.random() < p:
            edges.append((u, v, rng.randint(0, max_w), rng.randint(0, max_w)))
    return edges


def build(edges, n=None):
    # 预注册顶点 0..n-1，保证内部下标与 ID 一致（n 未给出时取边中最大 ID+1）。
    g = Graph()
    if n is None:
        n = 1 + max(max(u, v) for (u, v, _t, _c) in edges)
    for i in range(n):
        g.add_node(i)
    for i, (u, v, t, c) in enumerate(edges):
        g.add_arc(u, v, t, c, key=i)
    g.freeze()
    return g


def _graph_n(rng, n, p, max_w=4):
    """随机一般有向图（可能含环，含零权边）；保证顶点 0 和 n-1 已注册。"""
    edges = random_graph(rng, n, p, max_w=max_w)
    return build(edges, n=n)


@pytest.mark.parametrize("seed", range(20))
def test_dags_match_enumeration(seed):
    rng = random.Random(1000 + seed)
    n = rng.randint(4, 7)
    edges = random_dag(rng, n, p=rng.uniform(0.3, 0.7))
    g = build(edges, n=n)
    res = Solver(g).solve(0, n - 1)
    ref = reference_pareto(g, 0, n - 1)
    got = [(l.time, l.cost) for l in res.target_labels]
    assert fronts_equal_with_tol(ref, got), f"seed={seed} ref={ref} got={got}"
    if ref:
        assert res.status == "ok"
    else:
        assert res.status == "unreachable"


@pytest.mark.parametrize("seed", range(10))
def test_cyclic_graphs_match_enumeration(seed):
    rng = random.Random(2000 + seed)
    n = rng.randint(4, 6)
    edges = random_graph(rng, n, p=rng.uniform(0.2, 0.4))
    g = build(edges, n=n)
    res = Solver(g).solve(0, n - 1)
    ref = reference_pareto(g, 0, n - 1)
    got = [(l.time, l.cost) for l in res.target_labels]
    assert fronts_equal_with_tol(ref, got), f"seed={seed} ref={ref} got={got}"


@pytest.mark.parametrize("seed", range(24))
def test_cyclic_path_sets_match_enumeration(seed):
    """强校验：直接比较 Pareto 路径的 **顶点序列集合**（含环、含零权）。

    能抓住“同顶点集不同排列被错误合并/删除”的问题——只比较权重多重集会漏掉。
    """
    rng = random.Random(5000 + seed)
    n = rng.randint(4, 7)
    edges = random_graph(rng, n, p=rng.uniform(0.15, 0.45),
                         max_w=rng.choice([0, 1, 3, 6]))
    g = build(edges, n=n)
    tb = rng.choice([None, None, 3, 6])
    cb = rng.choice([None, None, 2])
    res = Solver(g).solve(0, n - 1, time_budget=tb, cost_budget=cb)
    ref_all = reference_pareto(g, 0, n - 1)
    ref = sorted(
        pp for pp, t, c in ref_all
        if (tb is None or t <= tb + 1e-9) and (cb is None or c <= cb + 1e-9)
    )
    got = sorted(lab.node_tuple() for lab in res.target_labels)
    assert ref == got, f"seed={seed} tb={tb} cb={cb}\nref={ref}\ngot={got}"


@pytest.mark.parametrize("seed", range(8))
def test_budgets_match_enumeration(seed):
    rng = random.Random(3000 + seed)
    n = rng.randint(4, 7)
    edges = random_dag(rng, n, p=0.6)
    g = build(edges, n=n)
    tb = rng.choice([None, 3, 5, 8])
    cb = rng.choice([None, 2, 6])
    res = Solver(g).solve(0, n - 1, time_budget=tb, cost_budget=cb)
    ref_all = reference_pareto(g, 0, n - 1)
    ref = [
        (p, t, c)
        for (p, t, c) in ref_all
        if (tb is None or t <= tb + 1e-9) and (cb is None or c <= cb + 1e-9)
    ]
    got = [(l.time, l.cost) for l in res.target_labels]
    assert fronts_equal_with_tol(ref, got), f"seed={seed} tb={tb} cb={cb}"


def test_reconstructed_path_exists_in_enumeration():
    """每条求解器返回的路径必须真实存在于枚举集合中（且权重相符）。"""
    rng = random.Random(4242)
    edges = random_dag(rng, 6, p=0.7)
    g = build(edges, n=6)
    res = Solver(g).solve(0, 5)
    all_paths = {p: (t, c) for p, t, c in _all(g, 0, 5)}
    for lab in res.target_labels:
        idx, arcs = lab.path()
        key = tuple(g.node_ids[i] for i in idx)
        assert key in all_paths
        t, c = all_paths[key]
        assert abs(t - lab.time) <= 1e-9 and abs(c - lab.cost) <= 1e-9


def _all(g, s, t):
    from .reference import enumerate_simple_paths

    return list(enumerate_simple_paths(g, s, t))
