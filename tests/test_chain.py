"""链传播、相关性策略与闭环检验测试。"""

import numpy as np
import pytest

from app.chain import (
    Edge,
    Factor,
    build_graph,
    bfs_path,
    chain_factors,
    evaluate_chain,
    evaluate_loop,
    fundamental_cycles,
    spanning_tree_poses,
)
from app.montecarlo import run_scenario
from app.se3 import inv_tf, se3_exp, se3_log

EPS = 1e-6


def _edge(i, T, C, version="v1"):
    return Edge(
        id=i,
        parent=f"n{i}",
        child=f"n{i + 1}",
        T=T,
        covariance=C,
        version=version,
        has_covariance=C is not None,
    )


def _fd_jacobian(factors, convention):
    pr = evaluate_chain(factors, convention)
    Jn = np.zeros((6, 6 * len(factors)))
    for k, f in enumerate(factors):
        for j in range(6):
            xe = np.zeros(6)
            xe[j] = EPS
            E = f.edge.T
            Ep = E @ se3_exp(xe) if convention == "right" else se3_exp(xe) @ E
            mats = [
                (inv_tf(Ep) if ff.inverse else Ep) if kk == k else ff.matrix
                for kk, ff in enumerate(factors)
            ]
            Pp = np.eye(4)
            for M in mats:
                Pp = Pp @ M
            d = (
                se3_log(inv_tf(pr.nominal) @ Pp)
                if convention == "right"
                else se3_log(Pp @ inv_tf(pr.nominal))
            )
            Jn[:, 6 * k + j] = d / EPS
    return Jn


@pytest.mark.parametrize("convention", ["right", "left"])
def test_jacobian_matches_finite_differences(convention):
    rng = np.random.default_rng(11)
    for trial in range(8):
        n = int(rng.integers(1, 7))
        factors = []
        for i in range(n):
            T = se3_exp(rng.normal(scale=0.4, size=6))
            e = Edge(
                id=f"e{i}",
                parent=f"a{i}",
                child=f"b{i}",
                T=T,
                covariance=np.eye(6) * 1e-6,
                version="v",
            )
            factors.append(Factor(edge=e, inverse=bool(rng.integers(0, 2))))
        J = evaluate_chain(factors, convention).jacobian
        Jn = _fd_jacobian(factors, convention)
        assert np.allclose(J, Jn, atol=5e-6, rtol=1e-4)


def _diag_chain(n=4, sig=1e-3, seed=1, missing=()):
    rng = np.random.default_rng(seed)
    factors = []
    for i in range(n):
        T = se3_exp(rng.normal(scale=0.3, size=6))
        C = None if i in missing else np.diag(
            [(0.5 * sig) ** 2] * 3 + [sig**2] * 3
        )
        e = Edge(
            id=f"e{i}", parent=f"n{i}", child=f"n{i + 1}", T=T,
            covariance=C, version="v1", has_covariance=i not in missing,
        )
        factors.append(Factor(edge=e, inverse=(i % 2 == 1)))
    return factors


@pytest.mark.parametrize("convention", ["right", "left"])
def test_first_order_propagation_vs_mc_small_angle(convention):
    factors = _diag_chain(4, 1e-3, seed=2)
    r = run_scenario(factors, convention, 20000, "small")
    assert r.covariance_status == "ok"
    assert r.fro_relative_error < 0.05
    assert abs(r.trace_ratio - 1.0) < 0.05


def test_long_chain_small_noise_still_accurate():
    factors = _diag_chain(12, 1e-3, seed=3)
    r = run_scenario(factors, "right", 20000, "long")
    assert r.fro_relative_error < 0.05


def test_large_noise_breaks_first_order():
    # 反例：σ=0.3 rad + 12 边，一阶近似明显偏离
    factors = _diag_chain(12, 3e-1, seed=4)
    r = run_scenario(factors, "right", 15000, "breakdown")
    assert r.fro_relative_error > 0.05


def test_missing_covariance_marks_unknown():
    factors = _diag_chain(4, 1e-3, seed=5, missing=(1,))
    r = run_scenario(factors, "right", 2000, "missing")
    assert r.covariance_status == "unknown"
    assert r.first_order_cov is None and r.sample_cov is None
    assert "缺失" in r.notes


def test_bounded_is_conservative_upper_bound():
    # 全正向链 + 等相关(ρ=0.5)公共扰动抽样：bounded(ρ=0.5) 输出空间
    # 上界应不低估样本方差（trace 比值 <= 1，留一点 MC 余量）。
    factors = _diag_chain_forward(4, 1e-3, seed=6)
    r = run_scenario(
        factors, "right", 30000, "bounded",
        policy="bounded", rho_max=0.5, rho_sample=0.5,
    )
    assert r.covariance_status == "ok"
    assert r.trace_ratio <= 1.05  # 样本/上界，允许 ~5% MC 波动
    pr = evaluate_chain(factors, "right")
    ind, _ = aggregate(factors, pr, "independent")
    # 同号相关时上界严格大于独立结果
    assert np.trace(r.first_order_cov) > np.trace(ind) * 1.5


def _diag_chain_forward(n=4, sig=1e-3, seed=1):
    rng = np.random.default_rng(seed)
    factors = []
    for i in range(n):
        T = se3_exp(rng.normal(scale=0.3, size=6))
        C = np.diag([(0.5 * sig) ** 2] * 3 + [sig**2] * 3)
        e = Edge(
            id=f"e{i}", parent=f"n{i}", child=f"n{i + 1}", T=T,
            covariance=C, version="v1", has_covariance=True,
        )
        factors.append(Factor(edge=e, inverse=False))
    return factors


def test_bounded_rho1_dominates_independent():
    factors = _diag_chain(4, 1e-3, seed=7)
    pr = evaluate_chain(factors, "right")
    ind, _ = aggregate(factors, pr, "independent")
    b1, _ = aggregate(factors, pr, "bounded", 1.0)
    # 保守模型必须 PSD 意义下不小于独立结果
    assert np.linalg.eigvalsh(b1 - ind).min() >= -1e-12


def aggregate(factors, pr, policy, rho=None):
    from app.chain import aggregate_covariance

    return aggregate_covariance(
        factors, pr.jacobian, policy=policy, rho_max=rho, cross_covs={}
    )


def test_correlation_increases_variance():
    factors = _diag_chain_forward(4, 1e-3, seed=8)
    pr = evaluate_chain(factors, "right")
    ind, _ = aggregate(factors, pr, "independent")
    b, _ = aggregate(factors, pr, "bounded", 0.8)
    assert np.trace(b) > np.trace(ind)


# --------------------------------------------------------------------------
# 图与闭环
# --------------------------------------------------------------------------


def _triangle_edges(Tab, Tbc, Tca, C):
    return [
        Edge("ab", "A", "B", Tab, C, "v1", True),
        Edge("bc", "B", "C", Tbc, C, "v1", True),
        Edge("ca", "C", "A", Tca, C, "v1", True),
    ]


def _g(edges):
    return build_graph(edges)


def test_bfs_shortest_path():
    C = np.eye(6) * 1e-6
    edges = [
        Edge("ab", "A", "B", np.eye(4), C, "v"),
        Edge("bc", "B", "C", np.eye(4), C, "v"),
        Edge("ac", "A", "C", np.eye(4), C, "v"),
    ]
    g = build_graph(edges)
    path = bfs_path(g, "A", "C")
    # 最短路径是直接边 ac（1 跳）
    assert [node for node, _ in path] == ["C"]
    assert bfs_path(g, "A", "A") == []
    # 不连通
    edges2 = edges + [Edge("xz", "X", "Z", np.eye(4), C, "v")]
    assert bfs_path(build_graph(edges2), "A", "Z") is None


def test_fundamental_cycles_triangle():
    C = np.eye(6) * 1e-6
    edges = _triangle_edges(np.eye(4), np.eye(4), np.eye(4), C)
    cycles = fundamental_cycles(build_graph(edges), "A")
    assert len(cycles) == 1
    cyc = cycles[0]
    assert cyc[0] == cyc[-1] and len(cyc) == 4


def test_closed_loop_no_conflict():
    C = np.diag([1e-8] * 6)
    Tab = se3_exp(np.array([1.0, 0, 0, 0, 0, 0]))
    Tbc = se3_exp(np.array([0.0, 1, 0, 0, 0, 0]))
    Tca = se3_exp(np.array([-1.0, -1, 0, 0, 0, 0]))
    edges = _triangle_edges(Tab, Tbc, Tca, C)
    g = build_graph(edges)
    cyc = fundamental_cycles(g, "A")[0]
    lr = evaluate_loop(
        cyc, g, "right", "independent", None, {}, 0.01
    )
    assert not lr.conflict
    assert lr.residual_norm < 1e-9
    assert lr.p_value > 0.01


def test_loop_conflict_detected_with_evidence():
    C = np.diag([1e-8] * 6)
    Tab = se3_exp(np.array([1.0, 0, 0, 0, 0, 0]))
    Tbc = se3_exp(np.array([0.0, 1, 0, 0, 0, 0]))
    Tca = se3_exp(np.array([-1.02, -1, 0, 0, 0, 0]))  # 2cm 误差
    edges = _triangle_edges(Tab, Tbc, Tca, C)
    g = build_graph(edges)
    cyc = fundamental_cycles(g, "A")[0]
    lr = evaluate_loop(cyc, g, "right", "independent", None, {}, 0.01)
    assert lr.conflict
    assert lr.p_value < 0.01
    # 最短证据路径就是该环的三条边
    assert set(lr.evidence_path) == {"edge:ab", "edge:bc", "edge:ca"}
    assert lr.versions == {"ab": "v1", "bc": "v1", "ca": "v1"}


def test_loop_missing_covariance_untestable():
    C = np.diag([1e-8] * 6)
    Tab = se3_exp(np.array([1.0, 0, 0, 0, 0, 0]))
    Tbc = se3_exp(np.array([0.0, 1, 0, 0, 0, 0]))
    Tca = se3_exp(np.array([-1.05, -1, 0, 0, 0, 0]))  # 明显误差但无法检验
    edges = [
        Edge("ab", "A", "B", Tab, C, "v1", True),
        Edge("bc", "B", "C", Tbc, None, "v1", False),
        Edge("ca", "C", "A", Tca, C, "v1", True),
    ]
    g = build_graph(edges)
    cyc = fundamental_cycles(g, "A")[0]
    lr = evaluate_loop(cyc, g, "right", "independent", None, {}, 0.01)
    assert lr.covariance_status == "unknown"
    assert lr.p_value is None and not lr.conflict
    assert "bc" in lr.missing_edges
    assert lr.residual_norm > 0.0  # 名义残差仍如实报告


def test_spanning_tree_propagates_poses():
    C = np.diag([1e-6] * 6)
    edges = [
        Edge("ab", "A", "B", se3_exp(np.array([1.0, 0, 0, 0, 0, 0])), C, "v"),
        Edge("bc", "B", "C", se3_exp(np.array([0.0, 1, 0, 0, 0, 0])), C, "v"),
    ]
    poses, issues = spanning_tree_poses(
        build_graph(edges), edges, "A", "right", "independent", None, {}
    )
    assert issues == []
    assert set(poses) == {"A", "B", "C"}
    assert poses["C"].covariance_status == "ok"
    # C 的位置约 (1,1,0)
    assert np.allclose(poses["C"].T_root_node[:3, 3], [1, 1, 0], atol=1e-9)
