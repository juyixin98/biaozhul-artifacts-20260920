"""标定图、协方差一阶传播与闭环检验。

约定
----
6 维扰动向量序为 ``[v; ω]``（前 3 平移、后 3 旋转）。

* 右扰动：``T' = T * Exp(ξ)``，边扰动定义在该边变换的「目标端(body)」帧；
* 左扰动：``T' = Exp(ξ) * T``，边扰动定义在该边变换的「源端(world)」帧。

设有序因子序列（有向边或其逆）乘积 ``P = F_1 F_2 ... F_n``，各因子带
6 维扰动 ``ξ_k``，则对乘积扰动 ``δ``（与边同一约定）一阶有::

    δ ≈ Σ_k J_k ξ_k,   J 为 6×6 块，于是  Σ = J blockdiag(Σ_k) Jᵀ
                                 （独立策略）

相关性策略
~~~~~~~~~~
* ``independent``：用户显式声明所有边扰动相互独立。不允许缺省沉默假设——
  API 层要求该字段必填，报告中回显该声明及涉及的边。
* ``bounded``：用户给定 ``rho_max ∈ [0,1]``，采用等相关(equicorrelated)
  模型::

      Cov(ξ_k, ξ_l) = rho_max (Σ_k^{1/2} Σ_l^{1/2ᵀ})  (k≠l)

  rho_max=1 对应沿每个方向的 Cauchy–Schwarz 保守上界（无方向交叉抵消）；
  rho_max<1 是带显式相关性上限的模型，用于缺信息时给出比独立更保守的
  不确定度，**绝不**用独立假设冒充。
* ``explicit``：用户通过 ``cross_covariances`` 提供逐对互协方差；未列出
  的边对按 ``unspecified_policy`` (independent/bounded) 处理，并在报告中
  显式列出。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

import numpy as np

from .se3 import adjoint, inv_tf, se3_log

DOF = 6


@dataclass
class Edge:
    """一条有向标定边 transform: parent -> child。"""

    id: str
    parent: str
    child: str
    T: np.ndarray  # 4x4 名义变换 T_parent_child
    covariance: np.ndarray | None  # 6x6 或 None（缺失）
    version: str
    has_covariance: bool = True


@dataclass
class Factor:
    """链上的一个有序因子：边（正向）或其逆。"""

    edge: Edge
    inverse: bool

    @property
    def matrix(self) -> np.ndarray:
        return inv_tf(self.edge.T) if self.inverse else self.edge.T


@dataclass
class PathResult:
    factors: list[Factor]
    nominal: np.ndarray
    jacobian: np.ndarray  # 6 x (6*n)，对因子顺序 [F1..Fn]
    missing_edges: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 图与路径
# ---------------------------------------------------------------------------


def build_graph(edges: list[Edge]) -> dict[str, list[tuple[str, Edge]]]:
    """邻接表：node -> [(neighbor, edge)]（无向图，边可双向走）。"""
    graph: dict[str, list[tuple[str, Edge]]] = {}
    for e in edges:
        graph.setdefault(e.parent, []).append((e.child, e))
        graph.setdefault(e.child, []).append((e.parent, e))
    return graph


def bfs_path(
    graph: dict[str, list[tuple[str, Edge]]], start: str, goal: str
) -> list[tuple[str, Edge]] | None:
    """最短路径（边数最少）。返回 [(node, edge), ...] 或 None。"""
    if start == goal:
        return []
    prev: dict[str, tuple[str, Edge] | None] = {start: None}
    q = deque([start])
    while q:
        u = q.popleft()
        for v, e in graph.get(u, []):
            if v not in prev:
                prev[v] = (u, e)
                if v == goal:
                    # 回溯
                    chain: list[tuple[str, Edge]] = []
                    cur: str = goal
                    while cur != start:
                        pu, pe = prev[cur]  # type: ignore[misc]
                        chain.append((cur, pe))
                        cur = pu
                    chain.reverse()
                    return chain
                q.append(v)
    return None


def chain_factors(
    chain: list[tuple[str, Edge]], start: str
) -> list[Factor]:
    """把 BFS 路径转为有序因子。

    chain 元素为 (到达节点, 所用边)。边名义方向 parent->child：
    从 parent 走到 child 用正向因子；从 child 走到 parent 用逆因子。
    """
    factors: list[Factor] = []
    prev_node = start
    for node, e in chain:
        if e.parent == prev_node and e.child == node:
            factors.append(Factor(edge=e, inverse=False))
        elif e.child == prev_node and e.parent == node:
            factors.append(Factor(edge=e, inverse=True))
        else:  # 理论不可达（图由该边连接两端）
            raise RuntimeError(
                f"path edge {e.id} does not connect {prev_node}->{node}"
            )
        prev_node = node
    return factors


def evaluate_chain(factors: list[Factor], convention: str) -> PathResult:
    """计算因子链名义乘积与扰动雅可比。

    P = F_1 ... F_n，扰动与边同一约定（全左或全右）。
    返回每 6 列一块的 ``J = [J_1 ... J_n]``，使 ``δ = Σ J_k ξ_k``。
    """
    n = len(factors)
    J = np.zeros((DOF, DOF * n))
    missing: list[str] = []

    # 名义乘积 P = F_1 ... F_n
    P = np.eye(4)
    for f in factors:
        P = P @ f.matrix

    # 各因子「边扰动 -> 因子自身扰动（同约定）」的局部映射 local_k
    locals_: list[np.ndarray] = []
    for f in factors:
        if convention == "right":
            # E'=E Exp(xe)。正向 F=E：因子扰动=xe；
            # 逆向 F=E^-1：F'=Exp(-xe)E^-1=F Exp(-Ad_E xe)，因子扰动=-Ad_E xe
            loc = np.eye(6) if not f.inverse else -adjoint(f.edge.T)
        else:
            # E'=Exp(xe) E。正向 F=E：因子左扰动=xe；
            # 逆向 F=E^-1：F'=F Exp(-xe)=Exp(-Ad_F xe) F，因子左扰动=-Ad_F xe
            loc = np.eye(6) if not f.inverse else -adjoint(inv_tf(f.edge.T))
        locals_.append(loc)
        if not f.edge.has_covariance:
            missing.append(f.edge.id)

    if convention == "right":
        # 因子 k 右扰动 eta_k（先经 local_k 由边扰动映射到因子扰动）：
        #   P' = (prefix) F_k Exp(eta_k) (tail)，tail=F_{k+1}...F_n
        #   P Exp(δ) = P tail⁻¹ Exp(eta_k) tail ⇒ δ = Ad_{tail⁻¹} eta_k
        tail = np.eye(4)  # 从最右端开始；F_n 的 tail = I，块系数 I
        for k in range(n - 1, -1, -1):
            J[:, 6 * k : 6 * (k + 1)] = (
                adjoint(inv_tf(tail)) @ locals_[k]
            )
            tail = factors[k].matrix @ tail
    else:  # left
        # 因子 k 的左扰动：F_1..F_{k-1} Exp(eta_k) F_k...
        #   = Exp(Ad_{F_1..F_{k-1}} eta_k) P
        head = np.eye(4)
        for k in range(n):
            J[:, 6 * k : 6 * (k + 1)] = adjoint(head) @ locals_[k]
            head = head @ factors[k].matrix

    return PathResult(
        factors=factors, nominal=P, jacobian=J, missing_edges=missing
    )


# ---------------------------------------------------------------------------
# 协方差聚合
# ---------------------------------------------------------------------------


def _psm_sqrt(A: np.ndarray) -> np.ndarray:
    """对称 PSD 平方根（对称版，使 Σ^{1/2} Σ^{1/2ᵀ} = A）。"""
    w, V = np.linalg.eigh(0.5 * (A + A.T))
    w = np.clip(w, 0.0, None)
    return (V * np.sqrt(w)) @ V.T


def aggregate_covariance(
    factors: list[Factor],
    J: np.ndarray,
    *,
    policy: str,
    rho_max: float | None,
    cross_covs: dict[tuple[str, str], np.ndarray],
) -> tuple[np.ndarray | None, dict]:
    """按相关性策略聚合链协方差。

    任一边协方差缺失 -> 返回 None（unknown），不猜测。
    cross_covs 的键为无序边 id 对（按字典序）。
    """
    n = len(factors)
    ids = [f.edge.id for f in factors]
    for f in factors:
        if not f.edge.has_covariance or f.edge.covariance is None:
            return None, {
                "status": "unknown",
                "missing_edges": [
                    f.edge.id
                    for f in factors
                    if not f.edge.has_covariance
                ],
            }

    Jks = [J[:, 6 * k : 6 * (k + 1)] for k in range(n)]
    covs = [f.edge.covariance for f in factors]  # type: ignore[union-attr]

    meta: dict = {"policy": policy}

    # 各边贡献投影到「链输出扰动」空间：A_k = J_k Σ_k J_kᵀ（6x6，PSD）。
    # 相关性交叉项必须在输出空间用 A_k^{1/2} A_l^{1/2} 构造，才能在任意
    # 方向满足 Cauchy–Schwarz（输入空间先开根再经 J 映射不具此性质）。
    blocks = [Jks[k] @ covs[k] @ Jks[k].T for k in range(n)]  # type: ignore[union-attr]
    block_sqrts = [_psm_sqrt(A) for A in blocks]
    Sigma = np.zeros((DOF, DOF))
    for A in blocks:
        Sigma += A

    cross_used: list[dict] = []
    bounded_pairs: list[list[str]] = []

    def add_output_cross(k: int, l: int, Bkl: np.ndarray, kind: str) -> None:
        nonlocal Sigma
        # Bkl 已在输出空间（对称 PSD 平方根的乘积），两项对称
        Sigma += Bkl + Bkl.T
        cross_used.append({"edges": [ids[k], ids[l]], "kind": kind})

    def add_explicit_cross(k: int, l: int, Ckl: np.ndarray) -> None:
        nonlocal Sigma
        # 互协方差在「边扰动」空间：输出交叉块为 J_k C_kl J_lᵀ
        B = Jks[k] @ Ckl @ Jks[l].T
        Sigma += B + B.T
        cross_used.append({"edges": [ids[k], ids[l]], "kind": "explicit"})

    if policy == "independent":
        pass  # 仅块对角项

    elif policy == "bounded":
        rho = float(rho_max) if rho_max is not None else 1.0
        for k in range(n):
            for l in range(k + 1, n):
                add_output_cross(
                    k, l, rho * (block_sqrts[k] @ block_sqrts[l]), "bounded_model"
                )
                bounded_pairs.append([ids[k], ids[l]])
        meta["rho_max"] = rho

    elif policy == "explicit":
        unspecified = str(meta.get("unspecified_policy", "independent"))
        rho = float(rho_max) if rho_max is not None else 1.0
        explicit_set = set()
        for k in range(n):
            for l in range(k + 1, n):
                key = tuple(sorted((ids[k], ids[l])))
                if key in cross_covs:
                    add_explicit_cross(k, l, cross_covs[key])
                    explicit_set.add(key)
                elif unspecified == "bounded":
                    add_output_cross(
                        k, l,
                        rho * (block_sqrts[k] @ block_sqrts[l]),
                        "bounded_model",
                    )
                    bounded_pairs.append([ids[k], ids[l]])
                # unspecified == independent: 不加
        meta["unspecified_policy"] = unspecified
        meta["explicit_pairs"] = [list(p) for p in sorted(explicit_set)]
    else:
        raise ValueError(f"unknown correlation policy: {policy}")

    meta["bounded_pairs"] = bounded_pairs
    meta["cross_terms_used"] = cross_used
    return 0.5 * (Sigma + Sigma.T), meta


# ---------------------------------------------------------------------------
# 生成树 / 闭环
# ---------------------------------------------------------------------------


@dataclass
class PoseEntry:
    node: str
    T_root_node: np.ndarray
    covariance: np.ndarray | None
    covariance_status: str  # ok | unknown
    missing_edges: list[str]
    path_edges: list[str]


def spanning_tree_poses(
    graph: dict[str, list[tuple[str, Edge]]],
    edges: list[Edge],
    root: str,
    convention: str,
    policy: str,
    rho_max: float | None,
    cross_covs: dict[tuple[str, str], np.ndarray],
) -> tuple[dict[str, PoseEntry], list[dict]]:
    """BFS 生成树，沿树路径传播名义变换与协方差。"""
    issues: list[dict] = []
    poses: dict[str, PoseEntry] = {
        root: PoseEntry(
            node=root,
            T_root_node=np.eye(4),
            covariance=np.zeros((6, 6)),
            covariance_status="ok",
            missing_edges=[],
            path_edges=[],
        )
    }
    # 记录父节点与树上的因子，用于逐级构建路径
    parent_of: dict[str, tuple[str, Edge]] = {}
    q = deque([root])
    seen = {root}
    while q:
        u = q.popleft()
        for v, e in graph.get(u, []):
            if v in seen:
                continue
            seen.add(v)
            parent_of[v] = (u, e)
            q.append(v)

    for node in sorted(seen - {root}):
        # 沿 parent 链拼路径
        chain: list[tuple[str, Edge]] = []
        cur = node
        while cur != root:
            pu, pe = parent_of[cur]
            chain.append((cur, pe))
            cur = pu
        chain.reverse()
        factors = chain_factors(chain, root)
        pr = evaluate_chain(factors, convention)
        cov, cmeta = aggregate_covariance(
            factors,
            pr.jacobian,
            policy=policy,
            rho_max=rho_max,
            cross_covs=cross_covs,
        )
        path_edges = [f.edge.id for f in factors]
        status = "ok" if cov is not None else "unknown"
        poses[node] = PoseEntry(
            node=node,
            T_root_node=pr.nominal,
            covariance=cov,
            covariance_status=status,
            missing_edges=cmeta.get("missing_edges", pr.missing_edges),
            path_edges=path_edges,
        )

    unreachable = set(graph) - seen
    for u in sorted(unreachable):
        issues.append(
            {
                "code": "DISCONNECTED_NODE",
                "message": f"节点 {u} 与根 {root} 不连通，无法传播",
                "evidence_path": [f"node:{u}"],
            }
        )
    return poses, issues


def fundamental_cycles(
    graph: dict[str, list[tuple[str, Edge]]], root: str
) -> list[list[str]]:
    """BFS 生成树之外的每条非树边（弦）构成一个基本环。

    对弦 (u,v)，环 = 弦 u->v ＋ 树上 v->u 的唯一路径。树路径经过
    LCA：v 上溯到 LCA，再沿链下行到 u。
    """
    # BFS 生成树，记录 parent 与深度
    parent: dict[str, tuple[str, Edge] | None] = {root: None}
    depth: dict[str, int] = {root: 0}
    q = deque([root])
    while q:
        u = q.popleft()
        for v, e in graph.get(u, []):
            if v not in parent:
                parent[v] = (u, e)
                depth[v] = depth[u] + 1
                q.append(v)

    tree_edge_ids = {
        pe[1].id for pe in parent.values() if pe is not None
    }

    cycles: list[list[str]] = []
    chord_seen: set[str] = set()
    for u, neighbors in graph.items():
        for v, e in neighbors:
            if e.id in tree_edge_ids or e.id in chord_seen:
                continue
            chord_seen.add(e.id)
            tree_nodes = _tree_path_via_lca(parent, depth, root, u, v)
            if tree_nodes is not None:
                # _tree_path_via_lca 已返回完整闭合序列 [u, v, ..., u]
                cycles.append(tree_nodes)
    return cycles


def _tree_path_via_lca(
    parent: dict[str, tuple[str, Edge] | None],
    depth: dict[str, int],
    root: str,
    u: str,
    v: str,
) -> list[str] | None:
    """闭合行走节点序列：先跨弦 u->v，再沿树 v->...->u（经过 LCA）。

    返回 ``[u, v, ..., lca, ..., u]``；相邻节点在图中都有边相连。
    """
    if u not in parent or v not in parent:
        return None

    def ancestors_to_lca(x: str, y: str) -> tuple[list[str], list[str]]:
        # 返回 x、y 各自到（含）LCA 的祖先链
        a, b = x, y
        ax, ay = [a], [b]
        while depth[a] > depth[b]:
            pa = parent[a]
            if pa is None:
                return [], []
            a = pa[0]
            ax.append(a)
        while depth[b] > depth[a]:
            pb = parent[b]
            if pb is None:
                return [], []
            b = pb[0]
            ay.append(b)
        while a != b:
            pa, pb = parent[a], parent[b]
            if pa is None or pb is None:
                return [], []
            a, b = pa[0], pb[0]
            ax.append(a)
            ay.append(b)
        return ax, ay  # 末元素都是 LCA

    chain_v, chain_u = ancestors_to_lca(v, u)
    if not chain_v or not chain_u:
        return None
    # 闭合行走节点序列 [u, v, ...lca..., (lca 与 u 之间)... u]：
    # 首步隐含弦 u->v，故序列以 u 起、以 u 终。
    # chain_v=[v, ..., lca]；lca 下行到 u 的中间节点（不含 lca、不含 u）
    # 由 chain_u=[u, parent(u), ..., lca] 去掉首元素 u 与末元素 lca 后逆序，
    # 最后再补上终点 u。
    down = list(reversed(chain_u[1:-1])) + [u]
    return [u] + chain_v + down


@dataclass
class LoopResult:
    nodes: list[str]
    edge_ids: list[str]
    residual: np.ndarray  # 6 维 log
    residual_norm: float
    rotation_angle: float
    covariance: np.ndarray | None
    covariance_status: str
    mahalanobis_sq: float | None
    p_value: float | None
    conflict: bool
    conflict_reason: str | None
    evidence_path: list[str]
    versions: dict[str, str]
    missing_edges: list[str]


def evaluate_loop(
    cycle_nodes: list[str],
    graph: dict[str, list[tuple[str, Edge]]],
    convention: str,
    policy: str,
    rho_max: float | None,
    cross_covs: dict[tuple[str, str], np.ndarray],
    alpha: float,
) -> LoopResult:
    """沿环顺序行走（每相邻节点取唯一连接边），计算闭环残差与检验。"""
    chain: list[tuple[str, Edge]] = []
    for a, b in zip(cycle_nodes, cycle_nodes[1:]):
        e = _edge_between(graph, a, b)
        chain.append((b, e))
    factors = chain_factors(chain, cycle_nodes[0])
    pr = evaluate_chain(factors, convention)

    L = pr.nominal  # 名义环乘积，闭合时应为 I
    residual = se3_log(L)
    res_norm = float(np.linalg.norm(residual))
    rot_angle = float(np.linalg.norm(residual[3:]))

    cov, cmeta = aggregate_covariance(
        factors,
        pr.jacobian,
        policy=policy,
        rho_max=rho_max,
        cross_covs=cross_covs,
    )
    edge_ids = [f.edge.id for f in factors]
    evidence = [f"edge:{i}" for i in edge_ids]
    versions = {f.edge.id: f.edge.version for f in factors}

    conflict = False
    reason: str | None = None
    maha_sq: float | None = None
    p_value: float | None = None
    status = "ok"
    missing = cmeta.get("missing_edges", pr.missing_edges)

    if cov is None:
        status = "unknown"
        reason = (
            "闭环含缺失协方差的边，无法做统计检验；仅报告名义残差，"
            "不判定冲突（不以独立假设替代）"
        )
    else:
        maha_sq, p_value = _mahalanobis_test(residual, cov)
        if p_value is None:
            reason = (
                "闭环协方差奇异（含零不确定方向），Mahalanobis 距离不可逆；"
                "报告名义残差"
            )
            status = "singular"
        elif p_value < alpha:
            conflict = True
            reason = (
                f"闭环 χ² 检验拒绝闭合：χ²={maha_sq:.4f}，"
                f"p={p_value:.3e} < α={alpha:g}（自由度 6）"
            )

    return LoopResult(
        nodes=list(cycle_nodes),
        edge_ids=edge_ids,
        residual=residual,
        residual_norm=res_norm,
        rotation_angle=rot_angle,
        covariance=cov,
        covariance_status=status,
        mahalanobis_sq=maha_sq,
        p_value=p_value,
        conflict=conflict,
        conflict_reason=reason,
        evidence_path=evidence,
        versions=versions,
        missing_edges=missing,
    )


def _edge_between(
    graph: dict[str, list[tuple[str, Edge]]], a: str, b: str
) -> Edge:
    for v, e in graph[a]:
        if v == b:
            return e
    # 平行边退化为任意一条的情况下，BFS 已保证连通；这里兜底
    raise RuntimeError(f"no edge between {a} and {b}")


def _mahalanobis_test(
    residual: np.ndarray, cov: np.ndarray
) -> tuple[float | None, float | None]:
    """6 维残差 Mahalanobis 距离与 χ²_6 上尾 p 值。

    χ²_6 生存函数有闭式::

        P(χ²_6 > x) = e^{-x/2}(1 + x/2 + x²/8)
    """
    try:
        w = np.linalg.eigvalsh(0.5 * (cov + cov.T))
    except np.linalg.LinAlgError:
        return None, None
    if w.min() <= 1e-12:
        # 奇异：在非零子空间投影检验
        if w.max() <= 1e-12:
            # 完全零协方差：残差非零即冲突，由调用方按名义残差处理
            return None, None
        V = np.linalg.eigh(0.5 * (cov + cov.T))[1]
        keep = w > 1e-10
        r_eff = V[:, keep].T @ residual
        w_eff = w[keep]
        x = float(r_eff @ np.diag(1.0 / w_eff) @ r_eff)
        dof = int(keep.sum())
        return x, chi2_sf(x, dof)
    x = float(residual @ np.linalg.solve(cov, residual))
    return x, chi2_sf(x, DOF)


def chi2_sf(x: float, dof: int) -> float:
    """χ² 生存函数（闭式伽马比的整数自由度特例，dof 偶数时多项式）。"""
    from math import exp

    x = max(x, 0.0)
    k = dof // 2
    if dof % 2 == 0:
        # P = e^{-x/2} Σ_{j=0}^{k-1} (x/2)^j / j!
        half = x / 2.0
        term = 1.0
        total = 1.0
        for j in range(1, k):
            term *= half / j
            total += term
        return min(1.0, exp(-half) * total)
    # 奇数自由度（本服务不用，保留完整正确性）：用正则化下不完全伽马
    raise NotImplementedError("only even-dof chi-square SF is implemented")
