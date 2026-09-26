"""Gauss-Newton / Levenberg-Marquardt 非线性最小二乘求解器。

目标函数::

    F(X) = Σ_edges 0.5 * ρ( sqrt(e_k^T Ω_k e_k) )

全局平移（x, y）与旋转（θ）三个自由度不可观，必须固定至少一个节点，
否则法方程奇异。求解器在进入迭代前检查：
1. 是否存在固定节点；
2. 图是否连通（不连通时非主分量无相对约束锚定，同样奇异）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .graph import PoseGraph, connected_components
from .kernels import kernel_weight, robust_cost
from .se2 import edge_jacobians, wrap_angle


@dataclass
class OptimizerOptions:
    """求解器参数。

    Attributes:
        max_iterations: 外层（IRLS/LM）最大迭代次数。
        tol_step: 位姿更新 L2 范数收敛阈值。
        tol_cost: 相对代价下降收敛阈值。
        initial_lambda: LM 阻尼初值。
        lambda_up: 拒绝步长时阻尼放大倍数。
        lambda_down: 接受步长时阻尼缩小倍数。
        min_lambda: 阻尼下限（纯 GN 极限）。
        verbose: 是否打印迭代日志。
    """

    max_iterations: int = 50
    tol_step: float = 1e-8
    tol_cost: float = 1e-9
    initial_lambda: float = 1e-3
    lambda_up: float = 10.0
    lambda_down: float = 3.0
    min_lambda: float = 1e-12
    verbose: bool = False

    def __post_init__(self) -> None:
        if self.max_iterations <= 0:
            raise ValueError("max_iterations 必须为正整数")
        if self.tol_step <= 0.0 or self.tol_cost <= 0.0:
            raise ValueError("收敛阈值必须为正数")
        if self.initial_lambda < 0.0 or self.lambda_up < 1.0:
            raise ValueError("阻尼参数非法（lambda>=0, lambda_up>=1）")
        if not 1.0 <= self.lambda_down:
            raise ValueError("lambda_down 必须 >= 1")


@dataclass
class OptimizeResult:
    """优化结果。

    注意：非线性最小二乘只保证收敛到局部最优，**不承诺全局最优**；
    当初始值远离正确解或错误约束过强时，可能收敛到其他局部极小点。
    """

    success: bool
    status: str
    iterations: int
    initial_cost: float
    final_cost: float
    poses: dict[int, np.ndarray]
    edge_chi2: dict[int, float] = field(default_factory=dict)
    weights: dict[int, float] = field(default_factory=dict)
    components: list[list[int]] = field(default_factory=list)
    cost_history: list[float] = field(default_factory=list)
    message: str = ""

    def to_summary(self) -> dict:
        return {
            "success": self.success,
            "status": self.status,
            "iterations": self.iterations,
            "initial_cost": self.initial_cost,
            "final_cost": self.final_cost,
            "cost_reduction_ratio": (
                1.0 - self.final_cost / self.initial_cost
                if self.initial_cost > 0.0
                else 0.0
            ),
            "components": [
                {"size": len(c), "node_ids": c} for c in self.components
            ],
            "message": self.message,
        }


class GraphNotSolvedError(ValueError):
    """图结构问题导致无法求解（无固定节点 / 图不连通等）。"""


def _validate_graph(graph: PoseGraph) -> tuple[list[list[int]], list[str]]:
    """结构校验：固定节点与连通性。返回 (连通分量, 问题列表)。"""
    problems: list[str] = []
    ids = graph.ordered_ids
    if not ids:
        problems.append("图为空：没有节点")
        return [], problems

    components = connected_components(ids, graph.edges)
    if graph.fixed_node_count() == 0:
        problems.append(
            "没有固定节点：SE2 存在 3 个全局自由度（x, y, theta）不可观，"
            "请至少固定一个节点（fixed=true）"
        )
    if len(components) > 1:
        detail = "; ".join(
            f"分量{k + 1}: {len(c)} 个节点 {c[:8]}"
            + ("..." if len(c) > 8 else "")
            for k, c in enumerate(components)
        )
        problems.append(
            f"图不连通：共 {len(components)} 个连通分量（{detail}）。"
            "不含固定节点的分量整体平移/旋转不可观，法方程将奇异；"
            "请补充连接边或为每个分量固定一个节点。"
        )
    else:
        # 连通但固定节点不在任何边上时，其余节点仍整体自由——
        # 连通性已排除这种情况（单节点图除外），此处给单节点快速路径。
        pass

    pd_problems = graph.check_positive_definite_info()
    problems.extend(pd_problems)
    return components, problems


def _compute_cost_and_weights(
    graph: PoseGraph, poses: dict[int, np.ndarray]
) -> tuple[float, dict[int, float], dict[int, float]]:
    total = 0.0
    chi2_map: dict[int, float] = {}
    weight_map: dict[int, float] = {}
    for edge in graph.edges:
        e = edge.residual(poses)
        u = float(e @ edge.info @ e)
        chi2_map[edge.id] = u
        w = kernel_weight(edge.kernel, u)
        weight_map[edge.id] = w
        total += robust_cost(edge.kernel, u)
    return total, chi2_map, weight_map


def _build_linear_system(
    graph: PoseGraph,
    poses: dict[int, np.ndarray],
    weight_map: dict[int, float],
    lam: float,
) -> tuple[np.ndarray, np.ndarray]:
    """组装鲁棒化（IRLS）后的法方程 ``(H + λ diag H) Δ = -b``。"""
    ids = graph.ordered_ids
    index = {nid: k for k, nid in enumerate(ids)}
    n = len(ids)
    dim = 3 * n
    h_mat = np.zeros((dim, dim))
    b_vec = np.zeros(dim)

    for edge in graph.edges:
        ai, aj = index[edge.i], index[edge.j]
        pi, pj = poses[edge.i], poses[edge.j]
        e = edge.residual(poses)
        jac_i, jac_j = edge_jacobians(pi, pj, edge.measurement)
        w = weight_map[edge.id]
        omega = w * edge.info

        h_ii = jac_i.T @ omega @ jac_i
        h_ij = jac_i.T @ omega @ jac_j
        h_jj = jac_j.T @ omega @ jac_j
        b_i = jac_i.T @ omega @ e
        b_j = jac_j.T @ omega @ e

        si, sj = 3 * ai, 3 * aj
        h_mat[si : si + 3, si : si + 3] += h_ii
        h_mat[sj : sj + 3, sj : sj + 3] += h_jj
        h_mat[si : si + 3, sj : sj + 3] += h_ij
        h_mat[sj : sj + 3, si : si + 3] += h_ij.T
        b_vec[si : si + 3] += b_i
        b_vec[sj : sj + 3] += b_j

    # 固定节点：行列清零、对角线置 1，对应块更新恒为 0
    for nid, node in graph.nodes.items():
        if node.fixed:
            k = index[nid]
            s = 3 * k
            h_mat[s : s + 3, :] = 0.0
            h_mat[:, s : s + 3] = 0.0
            h_mat[s : s + 3, s : s + 3] = np.eye(3)
            b_vec[s : s + 3] = 0.0

    # LM 阻尼（加在 H 自身对角线上；固定块已是单位阵，不受影响）
    damped = h_mat + lam * np.diag(np.diag(h_mat))
    return damped, -b_vec


def _apply_update(
    graph: PoseGraph,
    poses: dict[int, np.ndarray],
    delta: np.ndarray,
    index: dict[int, int],
) -> dict[int, np.ndarray]:
    new_poses = {nid: p.copy() for nid, p in poses.items()}
    for nid, k in index.items():
        if graph.nodes[nid].fixed:
            continue
        s = 3 * k
        new_poses[nid][0] += delta[s]
        new_poses[nid][1] += delta[s + 1]
        new_poses[nid][2] = wrap_angle(poses[nid][2] + delta[s + 2])
    return new_poses


def optimize(
    graph: PoseGraph, options: OptimizerOptions | None = None
) -> OptimizeResult:
    """对位姿图执行 LM 优化。

    Raises:
        GraphNotSolvedError: 图为空、无固定节点、图不连通或信息矩阵非正定。
    """
    options = options or OptimizerOptions()
    components, problems = _validate_graph(graph)
    if problems:
        raise GraphNotSolvedError("；".join(problems))

    ids = graph.ordered_ids
    index = {nid: k for k, nid in enumerate(ids)}
    poses = graph.initial_poses()

    cost, chi2_map, weight_map = _compute_cost_and_weights(graph, poses)
    initial_cost = cost
    history = [cost]
    lam = options.initial_lambda

    if options.verbose:
        print(f"[iter 0] cost={cost:.8f} lambda={lam:g}")

    status = "max_iterations_reached"
    iterations = 0
    for it in range(1, options.max_iterations + 1):
        iterations = it
        h_mat, rhs = _build_linear_system(graph, poses, weight_map, lam)
        try:
            delta = np.linalg.solve(h_mat, rhs)
        except np.linalg.LinAlgError as exc:
            return OptimizeResult(
                success=False,
                status="singular_system",
                iterations=it - 1,
                initial_cost=initial_cost,
                final_cost=cost,
                poses=poses,
                edge_chi2=chi2_map,
                weights=weight_map,
                components=components,
                cost_history=history,
                message=f"法方程奇异: {exc}",
            )

        step_norm = float(np.linalg.norm(delta))
        # 驻点判定：GN 步长近 0 说明梯度近 0（已在最优点，代价可能无法再严格
        # 下降，例如初始猜测恰好最优时代价恒为 0），直接判收敛。
        if step_norm < options.tol_step:
            status = "converged_step"
            if options.verbose:
                print(f"[iter {it}] cost={cost:.8f} step={step_norm:.3e} 收敛")
            break

        # 内层阻尼搜索：要求代价严格下降
        candidate = _apply_update(graph, poses, delta, index)
        new_cost, new_chi2, new_weights = _compute_cost_and_weights(
            graph, candidate
        )

        inner = 0
        while new_cost >= cost and inner < 30:
            lam *= options.lambda_up
            h_mat, rhs = _build_linear_system(graph, poses, weight_map, lam)
            delta = np.linalg.solve(h_mat, rhs)
            candidate = _apply_update(graph, poses, delta, index)
            new_cost, new_chi2, new_weights = _compute_cost_and_weights(
                graph, candidate
            )
            inner += 1

        if new_cost >= cost:
            status = "no_descent"
            if options.verbose:
                print(f"[iter {it}] 无法找到下降方向，停止")
            break

        rel_drop = (cost - new_cost) / max(abs(cost), 1e-30)
        poses = candidate
        chi2_map, weight_map = new_chi2, new_weights
        cost = new_cost
        history.append(cost)
        lam = max(options.min_lambda, lam / options.lambda_down)

        if options.verbose:
            print(
                f"[iter {it}] cost={cost:.8f} step={step_norm:.3e} "
                f"rel_drop={rel_drop:.3e} lambda={lam:g}"
            )

        if rel_drop < options.tol_cost:
            status = "converged_cost"
            break
    else:
        status = "max_iterations_reached"

    # 最终再算一次权重/残差，保证输出与最终位姿一致
    cost, chi2_map, weight_map = _compute_cost_and_weights(graph, poses)
    success = status.startswith("converged")
    messages = {
        "converged_step": "位姿更新小于阈值，收敛",
        "converged_cost": "相对代价下降小于阈值，收敛",
        "max_iterations_reached": "达到最大迭代次数（可能尚未完全收敛）",
        "no_descent": "无法继续降低代价",
    }
    return OptimizeResult(
        success=success,
        status=status,
        iterations=iterations,
        initial_cost=initial_cost,
        final_cost=cost,
        poses=poses,
        edge_chi2=chi2_map,
        weights=weight_map,
        components=components,
        cost_history=history,
        message=messages[status],
    )
