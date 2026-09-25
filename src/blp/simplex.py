"""两阶段单纯形法（表格式 / tableau 实现）。

约定（由 Gauss–Jordan 行变换唯一确定，避免两种教科书约定混用）
----------------------------------------------------------------

标准形 ``min c^T x, Ax=b, x>=0, b>=0``，初始基列为单位阵。
单纯形表 ``T`` 的前 ``m`` 行是典范等式约束，最后一列是右端项；
最后一行为目标行。初始化时把目标行写成

    z - c^T x = 0

即 ``T[-1, j] = -c_j``、``T[-1, -1] = 0``，再减去初始基费用行使
基列归零。对当前基 B（典范表体 Ā、右端 b̄），消元后的目标行满足：

    T[-1, j]  = c_B^T Ā_j - c_j =: d_j,
    T[-1, -1] = c_B^T b̄        =: z0。

Gauss–Jordan 枢轴（目标行减 d_q 倍枢轴行）后
``z0' = z0 - d_q (b̄_p/ā_pq)``，该格始终保存**当前目标值**。

对最小化问题：

* 所有 ``d_j <= 容差`` 时达到最优；
* ``d_j > 容差`` 的列可入基（目标以 d_j 为速率下降）；
* 若入基列在上半部没有正元素，则问题无界。

防循环
------
默认使用 Bland 规则（最小下标入基、比值相同时最小下标基列出基），
理论上保证有限步终止。另提供 Dantzig（最大检验数）规则用于暴露
循环风险；上层求解器在检测到基重复（循环）时会自动退回 Bland。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .model import MAX_ITERATIONS, TOL

RULES = ("bland", "dantzig", "largest_decrease")


@dataclass
class PhaseResult:
    """单个阶段的运行结果。"""

    status: str  # "optimal" | "unbounded" | "numeric" | "limit"
    iterations: int
    tableau: np.ndarray
    basis: np.ndarray
    entering: int | None = None
    repeated_basis: bool = False
    note: str = ""


def _coeff_scale(tableau: np.ndarray) -> float:
    """目标行系数的量级，用于相对容差。"""
    row = tableau[-1, :-1]
    return float(max(1.0, np.max(np.abs(row)) if row.size else 1.0))


def run_phase(
    tableau: np.ndarray,
    basis: np.ndarray,
    enter_columns: np.ndarray,
    rule: str = "bland",
    limit: int = MAX_ITERATIONS,
) -> PhaseResult:
    """在给定典范表上运行单纯形，直到最优 / 无界 / 失败。

    参数
    ------
    tableau:
        ``(m+1, k+1)`` 单纯形表，会被原地修改，调用方如需保留请传副本。
    basis:
        长度 m 的基列下标数组。
    enter_columns:
        允许入基的列下标（第一阶段排除人工列）。
    rule:
        见 :data:`RULES`。
    """
    if rule not in RULES:
        raise ValueError(f"未知枢轴规则 {rule!r}，可选 {RULES}")

    T = tableau
    m = basis.shape[0]
    basis = basis.copy()
    iterations = 0
    seen: set[bytes] = set()

    while True:
        # 循环检测：同一组基再次出现 => 退化下零步长转圈。
        sig = np.sort(basis).tobytes()
        if sig in seen:
            if rule == "bland":
                # Bland 规则下理论上不可能；出现说明数值已坏。
                return PhaseResult(
                    "numeric", iterations, T, basis,
                    repeated_basis=True,
                    note="Bland 规则下出现重复基，疑似数值精度崩溃",
                )
            return PhaseResult(
                "limit", iterations, T, basis,
                repeated_basis=True,
                note=f"检测到基重复（循环），规则={rule!r}",
            )
        seen.add(sig)

        d = T[-1, :-1]
        scale = _coeff_scale(T)
        rc_tol = max(TOL.reduced, 1.0e-10 * scale)

        candidates = enter_columns[d[enter_columns] > rc_tol]
        if candidates.size == 0:
            return PhaseResult("optimal", iterations, T, basis)

        if rule == "bland":
            q = int(candidates[0])
        elif rule == "dantzig":
            # 最大检验数；并列取最小下标（确定性）。
            best = d[candidates]
            q = int(candidates[np.argmax(best)])
        else:  # largest_decrease：按一步目标下降估值挑列
            q = _largest_decrease_column(T, basis, candidates)

        col = T[:m, q]
        b = T[:m, -1]

        # 右端出现明显负值 => 数值上已失去可行性。
        if np.any(b < -TOL.rhs_negative):
            return PhaseResult(
                "numeric", iterations, T, basis,
                note=f"右端项出现负值 {float(np.min(b)):.3e}，基不再可行",
            )

        pos = col > TOL.zero
        if not np.any(pos):
            return PhaseResult("unbounded", iterations, T, basis, entering=q)

        ratios = np.where(pos, b / np.where(pos, col, 1.0), np.inf)
        # 退化时 b 可能有微小负值，比值不应参与最小化。
        ratios = np.where(ratios < -TOL.feas, np.inf, ratios)
        finite = np.isfinite(ratios)
        if not np.any(finite):
            return PhaseResult("unbounded", iterations, T, basis, entering=q)
        r_min = np.min(ratios[finite])
        tie = np.flatnonzero(ratios <= r_min + TOL.feas * max(1.0, abs(r_min)))
        if rule == "bland":
            p = int(tie[np.argmin(basis[tie])])
        else:
            p = int(tie[0])

        pivot = T[p, q]
        if abs(pivot) < TOL.pivot:
            return PhaseResult(
                "numeric", iterations, T, basis,
                note=f"枢轴元素 {pivot:.3e} 小于 {TOL.pivot:.0e}",
            )

        _pivot(T, p, q)
        basis[p] = q
        iterations += 1
        if iterations > limit:
            return PhaseResult(
                "limit", iterations, T, basis,
                note=f"超过单阶段迭代上限 {limit}",
            )


def _pivot(T: np.ndarray, p: int, q: int) -> None:
    """以 (p, q) 为枢轴做一次 Gauss–Jordan 消元。"""
    pivot = T[p, q]
    T[p, :] /= pivot
    for i in range(T.shape[0]):
        if i != p and T[i, q] != 0.0:
            T[i, :] -= T[i, q] * T[p, :]


def _largest_decrease_column(
    T: np.ndarray, basis: np.ndarray, candidates: np.ndarray
) -> int:
    """选使一步目标下降量 -d_q * (b_p/a_pq) 最大的列。"""
    m = basis.shape[0]
    b = T[:m, -1]
    best_q = int(candidates[0])
    best_gain = -np.inf
    for q in candidates:
        col = T[:m, q]
        pos = col > TOL.zero
        if not np.any(pos):
            return int(q)  # 无界列优先级最高
        ratios = np.where(pos, b / np.where(pos, col, 1.0), np.inf)
        ratios = np.where(ratios < -TOL.feas, np.inf, ratios)
        if np.all(np.isinf(ratios)):
            return int(q)
        gain = float(-T[-1, q] * np.min(ratios))
        if gain > best_gain + 1.0e-12 * max(1.0, abs(best_gain)):
            best_gain = gain
            best_q = int(q)
    return best_q


# ---------------------------------------------------------------------------
# 第一阶段辅助表构造
# ---------------------------------------------------------------------------


def build_phase1_tableau(sf) -> tuple[np.ndarray, np.ndarray]:
    """根据标准形构造第一阶段问题。

    人工变量所在行的基列为单位阵；目标是最小化人工变量之和（人工列
    费用为 1，其余列为 0）。返回的目标行已化为关于初始基的典范形式：

        d_j = c_j - Σ(人工行 A_j),   z0 = Σ b(人工行)。
    """
    A = sf.Abar
    b = sf.bbar
    m, k = A.shape
    T = np.zeros((m + 1, k + 1))
    T[:m, :k] = A
    T[:m, -1] = b

    art = sf.artificial
    if art.size:
        col_to_row = {int(c): i for i, c in enumerate(sf.basis0)}
        art_rows = np.array([col_to_row[int(c)] for c in art], dtype=int)
        # z = Σ a_r = Σ(b_r - A_r x_struct)，用非基变量表达：
        # 入基（下降率）d_j = +Σ(人工行 A_j)；人工基列 0；
        # RHS = z0 = Σ b(人工行)。
        sums = A[art_rows, :].sum(axis=0)
        T[-1, :k] = sums
        T[-1, art] = 0.0
        T[-1, -1] = float(np.sum(b[art_rows]))
    # 无人工变量时目标行全零，第一阶段立即"最优"。
    return T, sf.basis0.copy()
