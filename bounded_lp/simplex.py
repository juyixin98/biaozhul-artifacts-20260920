"""两阶段单纯形法（自实现，仅依赖 NumPy 的稠密 Gauss–Jordan 表上作业）。

约定
----
标准形（见 :mod:`bounded_lp.problem`）为 ``min c^T x, Ax = b (b>=0), x >= 0``。

单纯形表 ``T`` 布局：第 0 行为检验数行，其后每一行对应一个有效等式；
最后一列为右端项。检验数行存储**最小化既约费用**

    T[0, j] = c_j - y^T A_j,      T[0, RHS] = -z = -y^T b

（其中 y^T = c_B B^{-1}，z = 当前目标值）。最优性条件为
**T[0, :] >= 0**；进入变量取检验数为负的列，最优时 RHS 的相反数
即最优目标值。基变量取在每行，``basis[r]`` 为该行基变量的列号。

两阶段
------
* **Phase I**：最小化人工变量之和 w。目标费用在非人工列为 0、人工列为 1。
  初始基是松弛/人工单位列，把人工基列上的检验数 1 用基行消为 0 后，
  非人工列检验数为 -1^T A、RHS 为 -w。
  - w* > feas_tol：原问题 **不可行**，由检验数行读出对偶 y，得到
    Farkas 不可行证书（y^T A <= 0, y^T b > 0）。
  - w* ≈ 0：若人工变量仍以零值占基，尝试非零元枢轴将其逐出；
    全零行则作为冗余 0=0 行删除（如重复等式）。
* **Phase II**：换入原目标检验数行后继续枢轴。
  - 无负检验数：**最优**；
  - 进入列在所有行上 <= 0：**无界**，由表直接构造改进射线。

防循环
------
默认使用 **Bland 规则**（进入列取下标最小的负检验数列；平局时离开行取
基变量下标最小者），Bland 规则保证有限步终止。另提供经典 Dantzig 规则
（检验数最负入基），仅用于实验/教学——经典 Beale 循环问题在该规则下
会触发 ``cycle_detected``（精确算术中）或 ``iteration_limit``（浮点）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from bounded_lp.errors import InvalidProblem
from bounded_lp.problem import LPProblem, StandardForm
from bounded_lp.tolerance import DEFAULT_TOLERANCE, MAX_ITERATIONS, Tolerance

# 求解状态
OPTIMAL = "optimal"
INFEASIBLE = "infeasible"
UNBOUNDED = "unbounded"
FAILED = "failed"

_TERMINAL_STATES = {OPTIMAL, INFEASIBLE, UNBOUNDED}


@dataclass
class SolveResult:
    """求解结果。

    成功状态为 ``optimal`` / ``infeasible`` / ``unbounded`` 之一；
    其余情况 ``status == "failed"`` 且 ``reason`` 给出失败原因：
    ``iteration_limit`` / ``cycle_detected`` / ``numerical_breakdown``。
    """

    status: str
    reason: str | None = None
    x: np.ndarray | None = None
    objective: float | None = None
    ray: np.ndarray | None = None
    objective_direction: float | None = None
    farkas_y: np.ndarray | None = None
    residuals: dict = field(default_factory=dict)
    phase1_iterations: int = 0
    phase2_iterations: int = 0
    rule: str = "bland"
    message: str = ""

    @property
    def ok(self) -> bool:
        """是否得到明确结论（最优/不可行/无界）。"""
        return self.status in _TERMINAL_STATES

    def to_dict(self) -> dict:
        """转为 JSON 友好的字典（numpy 类型转为原生类型）。"""

        def arr(a):
            return None if a is None else [float(v) for v in np.asarray(a).ravel()]

        out = {
            "status": self.status,
            "rule": self.rule,
            "iterations": {
                "phase1": self.phase1_iterations,
                "phase2": self.phase2_iterations,
                "total": self.phase1_iterations + self.phase2_iterations,
            },
            "message": self.message,
        }
        if self.reason is not None:
            out["reason"] = self.reason
        if self.x is not None:
            out["x"] = arr(self.x)
        if self.objective is not None:
            out["objective"] = float(self.objective)
        if self.ray is not None:
            out["ray"] = arr(self.ray)
        if self.objective_direction is not None:
            out["objective_direction"] = float(self.objective_direction)
        if self.farkas_y is not None:
            out["farkas_y"] = arr(self.farkas_y)
        if self.residuals:
            out["residuals"] = {k: float(v) for k, v in self.residuals.items()}
        return out


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------


def solve(
    problem: LPProblem,
    *,
    rule: str = "bland",
    max_iterations: int = MAX_ITERATIONS,
    tol: Tolerance = DEFAULT_TOLERANCE,
) -> SolveResult:
    """求解线性规划。

    Args:
        problem: 线性规划问题。
        rule: 枢轴规则，``"bland"``（默认，防循环）或 ``"dantzig"``。
        max_iterations: 两阶段主元迭代合计上限。
        tol: 数值容差。
    """
    if rule not in ("bland", "dantzig"):
        raise InvalidProblem(f"未知枢轴规则 {rule!r}，可选 bland / dantzig")
    if max_iterations < 1:
        raise InvalidProblem("max_iterations 必须 >= 1")

    sf = problem.standard_form()

    # 退化情形：没有任何有效约束行时，min c^T z, z>=0 直接判定
    if sf.A.shape[0] == 0:
        return _solve_unconstrained(sf, problem, rule, tol, extra_it1=0)

    return _two_phase(sf, problem, rule, max_iterations, tol)


# --------------------------------------------------------------------------
# 表上单纯形
# --------------------------------------------------------------------------


def _initial_tableau(sf: StandardForm) -> tuple[np.ndarray, np.ndarray]:
    """构造初始表与初始基（松弛列 + 人工列，均为单位列）。"""
    m, N = sf.A.shape
    T = np.zeros((m + 1, N + 1))
    T[1:, :N] = sf.A
    T[1:, N] = sf.b
    basis = np.asarray(sf.artificial_basis_cols, dtype=int).copy()

    # Phase I 费用 c_p1：非人工列 0、人工列 1。再用人工基行把基列
    # 上的检验数消为 0：等价于 T[0] = c_p1 - Σ(人工基行)。
    art_start = N - sf.n_artificial
    T[0, art_start:N] = 1.0
    for r, bc in enumerate(basis):
        if bc >= art_start:
            T[0] -= T[r + 1]
    return T, basis


def _pivot(T: np.ndarray, row: int, col: int) -> None:
    """以 T[row, col] 为主元做 Gauss–Jordan 消元（含检验数行）。"""
    pivot = T[row, col]
    T[row] /= pivot
    for r in range(T.shape[0]):
        if r != row:
            T[r] -= T[r, col] * T[row]


def _select_entering(T, active_cols, rule, reduced_tol):
    """返回进入列；无负检验数时返回 None。"""
    neg = [j for j in active_cols if T[0, j] < -reduced_tol]
    if not neg:
        return None
    if rule == "bland":
        return min(neg)
    return min(neg, key=lambda j: T[0, j])  # dantzig：检验数最负


def _select_leaving(T, basis, e, rule, tol):
    """最小比值检验。返回离开行；无界返回 None；右端崩坏返回 "breakdown"。"""
    best_row = None
    best_ratio = np.inf
    for r in range(1, T.shape[0]):
        a = T[r, e]
        if a <= tol.pivot_tol:
            continue
        b = T[r, -1]
        if b < -tol.feas_tol:
            return "breakdown"
        ratio = max(b, 0.0) / a
        if ratio < best_ratio - tol.pivot_tol:
            best_ratio, best_row = ratio, r
        elif abs(ratio - best_ratio) <= tol.pivot_tol and rule == "bland":
            # Bland：平局取基变量下标最小的行
            if best_row is None or basis[r - 1] < basis[best_row - 1]:
                best_row = r
    return best_row

def _simplex_loop(T, basis, entering_cols, *, rule, tol, budget, seen):
    """对给定表做枢轴迭代，原地修改 T/basis。

    返回 ``(state, iters, entering)``，state ∈
    optimal / unbounded / iteration_limit / cycle_detected / numerical_breakdown。
    ``budget`` 为长度 1 的剩余迭代计数器（原地消耗）。
    """
    iters = 0
    while True:
        key = frozenset(int(v) for v in basis)
        if key in seen:
            return "cycle_detected", iters, None
        seen.add(key)

        e = _select_entering(T, entering_cols, rule, tol.reduced_tol)
        if e is None:
            return "optimal", iters, None

        leaving = _select_leaving(T, basis, e, rule, tol)
        if leaving == "breakdown":
            return "numerical_breakdown", iters, None
        if leaving is None:
            return "unbounded", iters, e

        _pivot(T, leaving, e)
        basis[leaving - 1] = e
        iters += 1
        budget[0] -= 1
        if budget[0] <= 0:
            return "iteration_limit", iters, None


# --------------------------------------------------------------------------
# 两阶段驱动
# --------------------------------------------------------------------------


def _two_phase(sf, problem, rule, max_iterations, tol: Tolerance) -> SolveResult:
    m, N = sf.A.shape
    art_start = N - sf.n_artificial

    T, basis = _initial_tableau(sf)
    budget = [max_iterations]

    # ---- Phase I（人工列永不重新入基）----------------------------------
    state, it1, _ = _simplex_loop(
        T, basis, list(range(art_start)),
        rule=rule, tol=tol, budget=budget, seen=set(),
    )
    if state in ("iteration_limit", "cycle_detected", "numerical_breakdown"):
        return _failed(state, it1, 0, rule, "Phase I")
    if state == "unbounded":
        # Phase I 目标 w>=0 理论上不可能无界，出现即数值异常
        return _failed("numerical_breakdown", it1, 0, rule, "Phase I")

    w = -T[0, -1]                    # RHS 存 -w，故 w = -T[0,-1]
    if w > tol.feas_tol:
        # 不可行：检验数行 T[0] = c_p1 - y^T T[1:]，c_p1 在人工列为 1、
        # 其余为 0。初始基列的当前表列为 B^{-1} 的各列，故
        # y_i = c_p1[初始基列_i] - T[0,初始基列_i]。该 y 满足
        # y^T A <= 0, y^T b = w > 0（Farkas 证书）。
        y = np.array([
            (1.0 if bc >= art_start else 0.0) - T[0, bc]
            for bc in sf.artificial_basis_cols
        ])
        residuals = _farkas_residuals(sf, y)
        return SolveResult(
            status=INFEASIBLE,
            farkas_y=y,
            residuals=residuals,
            phase1_iterations=it1,
            rule=rule,
            message=f"Phase I 最优值 w*={_fmt(w)} > 容差 {tol.feas_tol:g}，系统不可行",
        )

    # ---- 逐出以零值占基的人工变量，删除冗余 0=0 行 ---------------------
    T, basis, keep_rows = _eject_artificials(T, basis, art_start, tol)
    sf_act = _compress_rows(sf, keep_rows)

    if keep_rows.size == 0:
        # 所有等式均退化为 0=0：实际无有效约束
        return _solve_unconstrained(sf_act, problem, rule, tol, extra_it1=it1)

    # ---- 删除人工列，进入 Phase II -------------------------------------
    keep_cols = list(range(art_start))
    T2 = np.zeros((T.shape[0], art_start + 1))
    T2[:, :art_start] = T[:, :art_start]
    T2[:, -1] = T[:, -1]
    basis2 = basis.copy()

    # 原目标检验数行：T[0] = c − Σ c_b · 基行（基列检验数消为 0）
    T2[0, :art_start] = sf.c[:art_start]
    for r, bc in enumerate(basis2):
        T2[0] -= float(sf.c[bc]) * T2[r + 1]

    state, it2, entering = _simplex_loop(
        T2, basis2, list(range(art_start)),
        rule=rule, tol=tol, budget=budget, seen=set(),
    )
    if state in ("iteration_limit", "cycle_detected", "numerical_breakdown"):
        return _failed(state, it1, it2, rule, "Phase II")

    x_std = _extract_solution(T2, basis2, N)

    if state == OPTIMAL:
        return _optimal_result(sf_act, problem, x_std, T2, it1, it2, rule, tol)

    # 无界：由终表构造标准形射线 d
    ray_std = np.zeros(N)
    ray_std[entering] = 1.0
    for r, bc in enumerate(basis2):
        ray_std[int(bc)] = -T2[r + 1, entering]
    return _unbounded_result(sf_act, problem, x_std, ray_std, it1, it2, rule, tol)


def _eject_artificials(T, basis, art_start, tol: Tolerance):
    """把人工变量从基中逐出；全零行标记为冗余行删除。

    返回 (新表, 新基, 保留的原行号数组)。
    """
    keep: list[int] = []
    for r in range(T.shape[0] - 1):
        if basis[r] < art_start:
            keep.append(r)
            continue
        # 在非人工列中找该行幅值最大的非零元做枢轴
        candidates = [
            j for j in range(art_start)
            if abs(T[r + 1, j]) > tol.pivot_tol
        ]
        if not candidates:
            continue                    # 冗余 0=0 行，删除
        j = max(candidates, key=lambda k: abs(T[r + 1, k]))
        _pivot(T, r + 1, j)
        basis[r] = j
        keep.append(r)

    keep_arr = np.asarray(keep, dtype=int)
    if keep_arr.size < T.shape[0] - 1:
        T = np.vstack([T[0:1], T[keep_arr + 1]])
        basis = basis[keep_arr]
    return T, basis, keep_arr


def _compress_rows(sf: StandardForm, keep_rows: np.ndarray) -> StandardForm:
    """返回只保留指定约束行的标准形（列、费用等不变）。"""
    return StandardForm(
        A=sf.A[keep_rows],
        b=sf.b[keep_rows],
        c=sf.c,
        obj_const=sf.obj_const,
        n_orig=sf.n_orig,
        lb=sf.lb,
        sense=sf.sense,
        n_slack=sf.n_slack,
        n_surplus=sf.n_surplus,
        n_artificial=sf.n_artificial,
        artificial_basis_cols=sf.artificial_basis_cols,
    )


def _extract_solution(T, basis, N_full) -> np.ndarray:
    """由终表提取标准形完整解（非基变量为 0）。"""
    x = np.zeros(N_full)
    for r, bc in enumerate(basis):
        x[int(bc)] = T[r + 1, -1]
    x[np.abs(x) < 1e-14] = 0.0
    return x


# --------------------------------------------------------------------------
# 结果组装与核验
# --------------------------------------------------------------------------


def _optimal_result(sf, problem, x_std, T2, it1, it2, rule, tol: Tolerance):
    x = sf.to_original_x(x_std)
    std_value = float(-T2[0, -1])         # RHS 存 -z
    objective = sf.original_objective(std_value)
    residuals = _original_residuals(problem, x)

    # 单纯形内部一致性：最小非基检验数；表上目标值 vs 直接代入
    min_rc = float(np.min(T2[0, :-1]))
    residuals["min_reduced_cost"] = min_rc
    residuals["reduced_cost_violation"] = max(0.0, -min_rc)
    residuals["objective_tableau_vs_direct"] = abs(
        sf.original_objective(float(-T2[0, -1])) - objective
    )
    # 直接代入标准形费用的独立核对
    residuals["objective_direct_vs_direct"] = abs(
        sf.original_objective(float(sf.c @ x_std)) - objective
    )

    msg = (
        f"找到最优解（{rule} 规则）：目标值={_fmt(objective)}，"
        f"最大约束残差={_fmt(residuals['max_abs_residual'])}"
    )
    return SolveResult(
        status=OPTIMAL, x=x, objective=objective,
        residuals=residuals, phase1_iterations=it1, phase2_iterations=it2,
        rule=rule, message=msg,
    )


def _unbounded_result(sf, problem, x_std, ray_std, it1, it2, rule, tol):
    x = sf.to_original_x(x_std)
    n = sf.n_orig
    ray = ray_std[:n]
    point_objective = sf.original_objective(float(sf.c @ x_std))
    # 标准形费用方向（应为严格负）；用户坐标下的方向
    cdir_std = float(sf.c @ ray_std)
    c_user = sf.c[:n] * (-1.0 if sf.sense == "max" else 1.0)
    cdir = float(c_user @ ray)

    residuals = _original_residuals(problem, x)
    residuals.update(_ray_residuals(problem, sf, ray, ray_std))
    residuals["ray_std_cost_direction"] = cdir_std

    msg = (
        f"问题无界（{rule} 规则）：沿返回射线目标可无限"
        f"{'增大' if sf.sense == 'max' else '减小'}，"
        f"标准形 c^T d = {_fmt(cdir_std)} < 0"
    )
    return SolveResult(
        status=UNBOUNDED, x=x, objective=point_objective, ray=ray,
        objective_direction=cdir, residuals=residuals,
        phase1_iterations=it1, phase2_iterations=it2, rule=rule, message=msg,
    )


def _farkas_residuals(sf: StandardForm, y: np.ndarray) -> dict:
    """核验 Farkas 证书：非人工列上 y^T A_j <= 0 且 y^T b > 0。"""
    art_start = sf.A.shape[1] - sf.n_artificial
    ya = sf.A[:, :art_start].T @ y
    return {
        "farkas_max_ytA": float(np.max(ya)),       # 应 <= 容差
        "farkas_ytb": float(y @ sf.b),             # 应 > 0
        "farkas_violation": float(max(0.0, np.max(ya))),
    }


def _original_residuals(problem: LPProblem, x: np.ndarray) -> dict:
    """针对用户原始问题计算可核验残差（绝对量，违反量一律截断到非负）。"""
    out: dict = {}
    if problem.A_ub.shape[0]:
        sl = problem.A_ub @ x - problem.b_ub
        out["ub_max_slack_or_violation"] = float(np.max(sl))  # <=0 为松弛，>0 为违反
        out["ub_violation"] = float(max(0.0, np.max(sl)))
    else:
        out["ub_max_slack_or_violation"] = 0.0
        out["ub_violation"] = 0.0
    if problem.A_eq.shape[0]:
        out["eq_abs_residual"] = float(np.max(np.abs(problem.A_eq @ x - problem.b_eq)))
    else:
        out["eq_abs_residual"] = 0.0
    out["lb_violation"] = float(max(0.0, np.max(problem.lb - x)))
    finite = np.isfinite(problem.ub)
    out["ub_bound_violation"] = (
        float(max(0.0, np.max(x[finite] - problem.ub[finite]))) if np.any(finite) else 0.0
    )
    out["max_abs_residual"] = max(
        out["ub_violation"], out["eq_abs_residual"],
        out["lb_violation"], out["ub_bound_violation"],
    )
    return out


def _ray_residuals(problem: LPProblem, sf: StandardForm,
                   ray: np.ndarray, ray_std: np.ndarray) -> dict:
    """无界射线的可核验残差（用户坐标 + 标准形坐标）。"""
    out: dict = {}
    if problem.A_ub.shape[0]:
        v = problem.A_ub @ ray
        out["ray_ub_max_direction"] = float(np.max(v))   # 应 <= 容差
    else:
        out["ray_ub_max_direction"] = 0.0
    if problem.A_eq.shape[0]:
        out["ray_eq_abs_residual"] = float(np.max(np.abs(problem.A_eq @ ray)))
    else:
        out["ray_eq_abs_residual"] = 0.0
    out["ray_lb_negative_component"] = float(max(0.0, -np.min(ray))) if ray.size else 0.0
    finite = np.isfinite(problem.ub)
    # 有限上界方向必须为 0（否则 t→∞ 必违反上界）
    out["ray_finite_ub_direction"] = (
        float(np.max(np.abs(ray[finite]))) if np.any(finite) else 0.0
    )
    # 标准形上的等式核验（仅对未删除的有效行 sf.A）
    out["ray_std_A_eq_residual"] = (
        float(np.max(np.abs(sf.A @ ray_std))) if sf.A.shape[0] else 0.0
    )
    out["ray_std_negative_component"] = float(max(0.0, -np.min(ray_std)))
    out["max_abs_residual"] = max(
        max(0.0, out["ray_ub_max_direction"]),
        out["ray_eq_abs_residual"],
        out["ray_lb_negative_component"],
        out["ray_finite_ub_direction"],
    )
    return out


def _solve_unconstrained(sf: StandardForm, problem: LPProblem, rule: str,
                         tol: Tolerance, *, extra_it1: int) -> SolveResult:
    """无有效约束行：min f^T z, z>=0（f 为标准形费用的原变量段）。"""
    n = sf.n_orig
    f = sf.c[:n]
    x_std = np.zeros_like(sf.c)
    neg = np.where(f < -tol.reduced_tol)[0]
    if neg.size == 0:
        x = sf.to_original_x(x_std)
        objective = sf.original_objective(0.0)
        residuals = _original_residuals(problem, x)
        return SolveResult(
            status=OPTIMAL, x=x, objective=objective, residuals=residuals,
            phase1_iterations=extra_it1, rule=rule,
            message="无有效约束且费用非负，原点即最优",
        )
    j = int(neg[0])
    ray_std = np.zeros_like(sf.c)
    ray_std[j] = 1.0
    return _unbounded_result(
        sf, problem, x_std, ray_std, extra_it1, 0, rule, tol
    )


def _failed(reason, it1, it2, rule, where) -> SolveResult:
    messages = {
        "iteration_limit": f"{where}达到迭代上限，未能得出结论",
        "cycle_detected": f"{where}检测到基组合重复（循环），未能得出结论",
        "numerical_breakdown": f"{where}出现数值崩坏（负右端/奇异枢轴），未能得出结论",
    }
    return SolveResult(
        status=FAILED, reason=reason,
        phase1_iterations=it1, phase2_iterations=it2, rule=rule,
        message=messages[reason],
    )


def _fmt(v: float) -> str:
    return f"{v:.6g}"
