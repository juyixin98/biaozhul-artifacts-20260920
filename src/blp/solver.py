"""两阶段单纯形求解器：标准化 → 第一阶段 → 第二阶段 → 结果回映。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

from .errors import LPNumericalError
from .model import LP, TOL, build_standard_form
from .simplex import build_phase1_tableau, run_phase


@dataclass
class SolveResult:
    """求解结果。

    status 取值
    -----------
    ``optimal`` / ``infeasible`` / ``unbounded`` /
    ``numeric_failure`` / ``iteration_limit``。
    """

    status: str
    objective: float | None = None
    x: np.ndarray | None = None
    ray: np.ndarray | None = None
    ray_objective_rate: float | None = None
    residuals: dict = field(default_factory=dict)
    certificate: dict | None = None
    iterations: tuple[int, int] = (0, 0)
    pivot_rule: str = "bland"
    warnings: list[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    detail: str = ""

    def to_dict(self) -> dict:
        d = {
            "status": self.status,
            "pivot_rule": self.pivot_rule,
            "iterations": {
                "phase1": self.iterations[0],
                "phase2": self.iterations[1],
                "total": self.iterations[0] + self.iterations[1],
            },
            "warnings": self.warnings,
            "elapsed_seconds": self.elapsed_seconds,
        }
        if self.status == "optimal":
            d["objective"] = self.objective
            d["x"] = None if self.x is None else _json_list(self.x)
            d["residuals"] = self.residuals
        if self.status == "unbounded":
            d["ray"] = None if self.ray is None else _json_list(self.ray)
            d["ray_objective_rate"] = self.ray_objective_rate
            d["witness"] = (
                None
                if self.x is None
                else _json_list(self.x + self.ray)
            )
            d["residuals"] = self.residuals
        if self.status == "infeasible":
            d["certificate"] = self.certificate
        if self.detail:
            d["detail"] = self.detail
        return d


def _json_list(a: np.ndarray) -> list[float]:
    return [float(v) for v in np.asarray(a).ravel()]


def solve_lp(lp: LP, pivot_rule: str = "bland") -> SolveResult:
    """求解一个 :class:`LP`。

    ``pivot_rule``: ``"bland"``（默认，防循环）、``"dantzig"``、
    ``"largest_decrease"`` 或 ``"auto"``（先 Bland）。
    """
    t0 = time.perf_counter()
    warnings: list[str] = []
    rule = pivot_rule

    sf = build_standard_form(lp)
    A = sf.Abar
    b = sf.bbar
    cbar = sf.cbar
    m, k = A.shape
    art_set = set(int(c) for c in sf.artificial)

    # ---- 退化情形：没有任何约束行 ----
    if m == 0:
        res = _unconstrained_nonnegative(lp, cbar, t0, warnings)
        return res

    T, basis = build_phase1_tableau(sf)
    enter_p1 = np.array([j for j in range(k) if j not in art_set], dtype=int)

    p1 = run_phase(T, basis, enter_p1, rule=rule)
    T, basis = p1.tableau, p1.basis
    if p1.status == "unbounded":
        return _fail(
            "numeric_failure",
            "第一阶段不应无界；请检查问题条件数",
            (p1.iterations, 0), rule, t0, warnings,
        )
    if p1.status == "numeric":
        return _fail("numeric_failure", p1.note, (p1.iterations, 0),
                     rule, t0, warnings)
    if p1.status == "limit":
        if p1.repeated_basis and rule != "bland":
            note = p1.note + "；已自动改用 Bland 规则重试"
            retried = solve_lp(lp, "bland")
            retried.warnings = warnings + [note] + retried.warnings
            return retried
        return _fail("iteration_limit", p1.note, (p1.iterations, 0),
                     rule, t0, warnings)

    b_scale = float(max(1.0, np.max(np.abs(b))))
    z0 = T[-1, -1]  # 目标行 RHS 即当前目标值
    if z0 > TOL.feas * b_scale:
        cert = _infeasibility_certificate(sf, T, basis)
        return SolveResult(
            status="infeasible",
            certificate=cert,
            iterations=(p1.iterations, 0),
            pivot_rule=rule,
            warnings=warnings,
            elapsed_seconds=time.perf_counter() - t0,
        )

    # ---- 第一阶段可行：把人工变量驱出基，删除冗余零行 ----
    T, basis, dropped = _drive_out_artificials(T, basis, art_set, warnings)

    keep_rows = np.array(
        [r for r in range(T.shape[0] - 1) if r not in dropped], dtype=int
    )
    keep_cols = np.array([j for j in range(k) if j not in art_set], dtype=int)
    if keep_rows.size == 0:
        # 所有行都是冗余零行：可行域只剩 y >= 0。
        T2 = None
        basis2 = np.empty(0, dtype=int)
    else:
        T_body = T[np.r_[keep_rows, -1], :][:, np.r_[keep_cols, k]]
        # 重新编号基列
        col_map = {int(old): new for new, old in enumerate(keep_cols)}
        basis2 = np.array(
            [col_map[int(c)] for c in basis[keep_rows]], dtype=int
        )
        T2 = T_body
        # 第二阶段目标行（d = c_B^T Ā - c，RHS = c_B^T b̄ = z0）。
        m2 = basis2.shape[0]
        cB = cbar[keep_cols][basis2]
        body = T2[:m2, : keep_cols.size]
        d = cB @ body - cbar[keep_cols]
        d[basis2] = 0.0
        T2[-1, : keep_cols.size] = d
        T2[-1, -1] = float(cB @ T2[:m2, -1])  # RHS = z0

    n2 = keep_cols.size
    enter_p2 = np.arange(n2)

    if T2 is None:
        # 无约束的非负最小化：检验数符号直接判定。
        res = _unconstrained_nonnegative(
            lp, cbar[keep_cols], t0, warnings,
            iters=(p1.iterations, 0),
        )
        return res

    p2 = run_phase(T2, basis2, enter_p2, rule=rule)
    if p2.status == "numeric":
        return _fail("numeric_failure", p2.note,
                     (p1.iterations, p2.iterations), rule, t0, warnings)
    if p2.status == "limit":
        if p2.repeated_basis and rule != "bland":
            note = p2.note + "；已自动改用 Bland 规则重试"
            retried = solve_lp(lp, "bland")
            retried.warnings = warnings + [note] + retried.warnings
            return retried
        return _fail("iteration_limit", p2.note,
                     (p1.iterations, p2.iterations), rule, t0, warnings)

    # ---- 提取结果 ----
    if p2.status == "unbounded":
        return _build_unbounded(
            lp, sf, p2, keep_cols, (p1.iterations, p2.iterations),
            rule, t0, warnings,
        )

    return _build_optimal(
        lp, sf, p2, keep_cols, (p1.iterations, p2.iterations),
        rule, t0, warnings,
    )


# ---------------------------------------------------------------------------


def _drive_out_artificials(T, basis, art_set, warnings):
    """把（取值≈0 的）人工基变量用非人工列替换；替换不掉的零行待删。"""
    m = basis.shape[0]
    dropped: set[int] = set()
    # 只在人工基行上找枢轴，迭代到不再变化。
    changed = True
    while changed:
        changed = False
        for r in range(m):
            if r in dropped or int(basis[r]) not in art_set:
                continue
            row = T[r, :-1]
            # 选绝对值最大的非人工非零元素作枢轴（数值更稳）。
            candidates = [
                j for j in range(row.size)
                if j not in art_set and abs(row[j]) > TOL.pivot
            ]
            if not candidates:
                continue
            j = max(candidates, key=lambda jj: abs(row[jj]))
            if abs(T[r, -1]) > TOL.feas:
                # 理论上 z0≈0 时不可能；出现即数值问题。
                raise LPNumericalError(
                    "人工变量驱出基时发现正的右端项，第一阶段最优值核验矛盾"
                )
            from .simplex import _pivot
            _pivot(T, r, j)
            basis[r] = j
            changed = True

    for r in range(m):
        if int(basis[r]) in art_set:
            if abs(T[r, -1]) > TOL.feas:
                raise LPNumericalError(
                    "人工变量保持为正基变量，但第一阶段目标声称可行"
                )
            dropped.add(r)
            warnings.append(f"删除冗余约束行（标准化后第 {r} 行为零行）")
    return T, basis, dropped


def _unconstrained_nonnegative(lp, cbar, t0, warnings, iters=(0, 0)):
    """可行域只有 y >= 0（无有效约束）时的闭式判定。

    内部目标系数 cbar 已按 min 口径（max 时取反）。
    起始基本解是 y=0，即原始变量 x = shift（下界点）。
    """
    n = lp.n
    y = np.zeros(cbar.shape[0])
    min_c = float(np.min(cbar))
    if min_c >= -TOL.reduced:
        return _optimal_from_y(lp, y[:n], iters, "bland", t0, warnings)
    j = int(np.argmin(cbar))
    ray_y = np.zeros(n)
    ray_y[j] = 1.0
    shift = getattr(lp, "shift", np.zeros(n))
    x0 = shift.copy()
    for j_fix, v in lp.fixed.items():
        x0[j_fix] = v
    # 变化率按"原目标沿射线的变化"报告：min 时应为负，max 时应为正。
    rate = float(lp.c @ ray_y)
    res = SolveResult(
        status="unbounded",
        x=x0,
        ray=ray_y,
        ray_objective_rate=rate,
        iterations=iters,
        pivot_rule="bland",
        warnings=warnings,
        elapsed_seconds=time.perf_counter() - t0,
    )
    return res


def _build_optimal(lp, sf, p2, keep_cols, iters, rule, t0, warnings):
    T = p2.tableau
    basis = p2.basis
    m2 = basis.shape[0]
    y_full = np.zeros(keep_cols.size)
    for r in range(m2):
        y_full[basis[r]] = T[r, -1]
    n = lp.n
    y = np.zeros(n)
    for new_j, old_j in enumerate(keep_cols):
        if old_j < n:
            y[old_j] = y_full[new_j]
    # 清除 -0 与微小负零。
    y[np.abs(y) < TOL.zero] = 0.0
    return _optimal_from_y(lp, y, iters, rule, t0, warnings,
                           z_tableau=float(T[-1, -1]))


def _optimal_from_y(lp, y, iters, rule, t0, warnings, z_tableau=None):
    shift = getattr(lp, "shift", np.zeros(lp.n))
    x = y + shift
    for j, v in lp.fixed.items():
        x[j] = v
    # 目标值直接由原始目标重算（独立于表，便于核验）；c0 已含平移常数。
    obj = float(lp.c @ x) + lp.c0
    res = SolveResult(
        status="optimal",
        objective=obj,
        x=x,
        iterations=iters,
        pivot_rule=rule,
        warnings=warnings,
        elapsed_seconds=time.perf_counter() - t0,
    )
    return res


def _build_unbounded(lp, sf, p2, keep_cols, iters, rule, t0, warnings):
    T = p2.tableau
    basis = p2.basis
    q_new = p2.entering
    m2 = basis.shape[0]
    full = np.zeros(keep_cols.size)
    col = T[:m2, q_new]
    for r in range(m2):
        full[basis[r]] = -col[r]
    full[q_new] = 1.0
    n = lp.n
    y = np.zeros(n)
    dy = np.zeros(n)
    for new_j, old_j in enumerate(keep_cols):
        if old_j < n:
            y[old_j] = max(0.0, _safe_rhs(T, basis, new_j, m2))
            dy[old_j] = full[new_j]
    dy[np.abs(dy) < TOL.zero] = 0.0
    shift = getattr(lp, "shift", np.zeros(n))
    x = y + shift
    for j, v in lp.fixed.items():
        x[j] = v
    rate_internal = float(sf.cbar[keep_cols] @ full)  # 最小化口径，应 < 0
    if lp.sense == "min":
        rate = float(lp.c @ dy)
    else:
        rate = float(lp.c @ dy)  # 最大化口径，应 > 0
    if lp.sense == "min" and rate >= -TOL.reduced:
        warnings.append("无界射线的目标变化率核验异常（>=0）")
    if lp.sense == "max" and rate <= TOL.reduced:
        warnings.append("无界射线的目标变化率核验异常（<=0）")
    return SolveResult(
        status="unbounded",
        x=x,
        ray=dy,
        ray_objective_rate=rate,
        iterations=iters,
        pivot_rule=rule,
        warnings=warnings,
        elapsed_seconds=time.perf_counter() - t0,
        detail=f"入基列（标准化后编号）={int(keep_cols[q_new])}，"
               f"内部目标变化率={rate_internal:.6g}",
    )


def _safe_rhs(T, basis, new_j, m2):
    for r in range(m2):
        if basis[r] == new_j:
            return T[r, -1]
    return 0.0


def _infeasibility_certificate(sf, T, basis) -> dict:
    r"""构造不可行的 Farkas 证书，并在原始行系统上可独立核验。

    标准化后的等式系统为 ``Abar z = bbar, z >= 0``（结构列、松弛列、
    人工列全部是非负变量）。第一阶段费用 c* 仅在人工列为 1。最终基 B
    （**取自原始 Abar，不是典范表体**）的对偶 u 满足

        u^T = c_*^T B^{-1},   即 B^T u = c_B。

    本表检验数 ``d_j = u^T Abar_j - c_j*``；非人工列 c_j*=0，故
    ``d_j = u^T Abar_j``。第一阶段最优 ⇒ ``d_j <= 0``，即
    ``u^T Abar_j <= 0``；最优值 ``z0 = u^T bbar > 0``。

    对用户输出原问题行（``A_ub x <= b_ub``、``A_eq x = b_eq``、
    ``x >= 0``）的乘子 ``lambda = -u``：

      * ``<=`` 行：``lambda_i = -u_i >= 0``；``=`` 行：乘子自由；
      * 结构列：``lambda^T A_struct = -u^T A_struct >= 0``；
      * 常数：``lambda^T bbar = -z0 < 0``。

    这正是 Farkas 替代定理的不可行证书：若原系统可行，则
    ``lambda^T bbar = (A_struct^T lambda)^T x + (松弛项) >= 0``，
    与严格负矛盾。函数末尾独立核验全部条件。
    """
    m = basis.shape[0]
    art_set = set(int(c) for c in sf.artificial)
    B0 = sf.Abar[:, basis]  # 原始坐标下的基矩阵
    cB = np.array([1.0 if int(c) in art_set else 0.0 for c in basis])
    try:
        u = np.linalg.solve(B0.T, cB)
    except np.linalg.LinAlgError as exc:
        raise LPNumericalError(f"证书：基矩阵奇异：{exc}") from exc
    lam = -u

    nonart = np.array(
        [j for j in range(sf.Abar.shape[1]) if j not in art_set], dtype=int
    )
    d = T[-1, :-1]
    # 非人工列：d_j = u^T Abar_j。
    resid_d = (
        float(np.max(np.abs(u @ sf.Abar[:, nonart] - d[nonart])))
        if nonart.size else 0.0
    )
    # 在"原始行系统"上核验：对每个结构变量列 j，
    #   Σ_r mu_r * A_orig[r, j] >= 0
    # （翻转行 A_orig = -Abar 行，与 mu = sgn*lam 的还原一致）。
    struct = np.arange(sf.n_orig)
    signs = np.array([sgn for _, _, sgn in sf.row_kinds], dtype=float)
    mu = signs * lam
    A_orig = sf.Abar[:, struct] * signs[:, None]
    Atmu_s = A_orig.T @ mu if struct.size else np.zeros(0)
    min_struct = float(np.min(Atmu_s)) if struct.size else 0.0
    scale = float(max(1.0, np.max(np.abs(d[nonart])) if nonart.size else 1.0))

    # <= 类行原始乘子必须非负；等式行自由。
    min_ub_mult = min(
        (mu[r] for r, (k, _, _) in enumerate(sf.row_kinds) if k != "eq"),
        default=0.0,
    )

    def _orig_rhs(k, idx):
        # 所有行都在"平移后规范系统"口径下（与 verify 模块重建一致）：
        # ub/bound 行的右端是有效界 lp.ub；调用方的独立核验也用该值。
        if k == "ub":
            return sf.lp.b_ub[idx]
        if k == "eq":
            return sf.lp.b_eq[idx]
        return float(sf.lp.ub[idx])

    # 矛盾常数：标准形上 lam^T bbar 与原始口径 mu^T b_orig 相等
    # （翻转行两侧同时取反），都必须严格为负。
    ltb = float(lam @ sf.bbar)
    mu_tb_orig = float(
        sum(
            mu[r] * _orig_rhs(k, idx)
            for r, (k, idx, _) in enumerate(sf.row_kinds)
        )
    )
    if (
        resid_d > TOL.cert * scale
        or min_struct < -TOL.cert
        or min_ub_mult < -TOL.cert
        or ltb >= -TOL.cert
        or abs(ltb - mu_tb_orig) > TOL.cert * max(1.0, abs(ltb))
    ):
        raise LPNumericalError(
            "不可行证书核验失败："
            f"检验数残差 {resid_d:.3e}，"
            f"min(mu^T A_orig)={min_struct:.3e}，"
            f"<=行乘子最小值 {min_ub_mult:.3e}，"
            f"lam^Tbbar={ltb:.3e}，mu^Tb_orig={mu_tb_orig:.3e}"
        )

    entries = [
        {
            "kind": kind,
            "index": int(idx),
            "row_flipped": sgn == -1,
            "multiplier": float(mu[r]),
        }
        for r, (kind, idx, sgn) in enumerate(sf.row_kinds)
    ]
    return {
        "convention": (
            "原系统 A_ub x <= b_ub, A_eq x = b_eq, x >= 0 的 Farkas "
            "证书 mu：<= 行乘子 >= 0、= 行乘子自由；"
            "mu^T A_struct >= 0（逐结构变量列）；mu^T b_orig < 0。"
            "若 x 可行则 mu^T b_orig >= 0，矛盾。行翻转在输出时已还原。"
        ),
        "rows": entries,
        "muTb": mu_tb_orig,
        "min_struct_Atmu": min_struct,
        "min_ineq_multiplier": float(min_ub_mult),
        "reduced_cost_residual": resid_d,
    }


def _fail(status, detail, iters, rule, t0, warnings):
    return SolveResult(
        status=status,
        detail=detail,
        iterations=iters,
        pivot_rule=rule,
        warnings=warnings,
        elapsed_seconds=time.perf_counter() - t0,
    )
