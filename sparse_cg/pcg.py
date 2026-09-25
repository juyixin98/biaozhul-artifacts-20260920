"""预条件共轭梯度法（Preconditioned Conjugate Gradient, PCG）。

适用范围：对称正定（SPD）稀疏线性系统 ``A x = b``。
算法为标准 PCG（可选 Jacobi（对角）预条件），并在标准实现之外加入三类
数值健康监控，全部基于**真实残差** r_true = b - A x 而非仅递推残差：

1. 收敛判据
       ||r_true||_2 <= atol + rtol * ||b||_2
   递推残差到达阈值时，必重算真实残差确认后才宣布收敛；每隔
   ``restart_period`` 次迭代做一次周期性真实残差检查。

2. 停滞（stagnation）
   连续 ``stagnation_patience`` 次周期性真实残差检查，真实残差相对历史
   最好值的下降都不足因子 ``stagnation_factor``，判定停滞并返回
   ``stagnated``。

3. 非正曲率 / 预条件失效
   每次迭代计算瑞利商 (pᵀAp)/(pᵀp)（与方向尺度无关，SPD 时必 > 0）：
       瑞利商 < -tol·||A|| -> ``negative_curvature``（矩阵非正定）
       |瑞利商| <= tol·||A|| -> ``zero_curvature``（奇异方向）
   Jacobi 预条件在对角元非正处无法取逆（输入错误，抛 RequestError），
   对角元过小到接近下溢则返回 ``preconditioner_breakdown``。

周期性检查同时做**残差替换**（residual replacement）：用真实残差校正
递推残差以抑制舍入误差累积，但延续原搜索方向与 β 递推（不整体重启，
保持 CG 的共轭性与有限终止性）。

其余失败状态：``max_iterations``（迭代用尽）、``diverged``（残差非有限
或超过初值的发散因子）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .csr import CSRMatrix
from .errors import RequestError

# ---- 算法默认数值参数（README「数值约定」中记录） -------------------------
DEFAULT_RTOL = 1e-8            # 相对残差收敛容差
DEFAULT_ATOL = 1e-12           # 绝对残差收敛容差（兼顾零右端）
DEFAULT_MAX_ITER_MULT = 10     # max_iter 默认倍数（相对于 n）
MAX_ITER_LIMIT = 100_000       # max_iter 硬上限（中小规模定位）
DEFAULT_RESTART_PERIOD = 30    # 每隔多少次迭代做一次真实残差检查/替换
DEFAULT_STAG_PATIENCE = 3      # 连续多少次真实检查无进展判停滞
DEFAULT_STAG_FACTOR = 0.999    # 残差需低于 历史最好 * factor 才算有进展
CURVATURE_TOL = 1e-12          # 瑞利商相对零的判据（乘以矩阵量级）
DIVERGENCE_FACTOR = 1e8        # 残差超过初值该倍数判定发散

STATUS_CONVERGED = "converged"
STATUS_MAX_ITER = "max_iterations"
STATUS_NEG_CURV = "negative_curvature"
STATUS_ZERO_CURV = "zero_curvature"
STATUS_STAGNATED = "stagnated"
STATUS_PRECOND_BREAK = "preconditioner_breakdown"
STATUS_DIVERGED = "diverged"

FAILING_STATUSES = frozenset(
    {
        STATUS_MAX_ITER,
        STATUS_NEG_CURV,
        STATUS_ZERO_CURV,
        STATUS_STAGNATED,
        STATUS_PRECOND_BREAK,
        STATUS_DIVERGED,
    }
)


@dataclass
class PCGResult:
    """PCG 求解结果。

    Attributes:
        status: 收敛或失败状态码（见模块常量）。
        message: 状态的人类可读说明。
        x: 退出时的近似解（失败时也返回当前迭代值）。
        iterations: 已执行的迭代次数。
        residual_norm: 退出时**真实残差** ||b - A x||_2。
        initial_residual_norm: 初始真实残差（x0 处）。
        relative_residual: residual_norm / ||b||（||b||=0 时记残差本身）。
        residual_history: 每次真实残差检查的残差范数序列（含初始值）。
        converged: 是否达到收敛容差。
    """

    status: str
    message: str
    x: np.ndarray
    iterations: int
    residual_norm: float
    initial_residual_norm: float
    relative_residual: float
    residual_history: list[float] = field(default_factory=list)

    @property
    def converged(self) -> bool:
        return self.status == STATUS_CONVERGED

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "converged": self.converged,
            "message": self.message,
            "iterations": int(self.iterations),
            "residual_norm": float(self.residual_norm),
            "initial_residual_norm": float(self.initial_residual_norm),
            "relative_residual": float(self.relative_residual),
            "residual_history": [float(v) for v in self.residual_history],
            "x": [float(v) for v in self.x],
        }


def _validate_options(
    rtol: float,
    atol: float,
    max_iter: int | None,
    n: int,
    restart_period: int,
    stagnation_patience: int,
    stagnation_factor: float,
) -> tuple[float, float, int]:
    """校验并归一化收敛参数。"""
    if not np.isfinite(rtol) or rtol < 0:
        raise RequestError("invalid_rtol", f"rtol 必须为非负有限数，实际 {rtol!r}")
    if not np.isfinite(atol) or atol < 0:
        raise RequestError("invalid_atol", f"atol 必须为非负有限数，实际 {atol!r}")
    if max_iter is None:
        max_iter = max(1000, DEFAULT_MAX_ITER_MULT * n)
    elif not isinstance(max_iter, int) or isinstance(max_iter, bool) or max_iter <= 0:
        raise RequestError(
            "invalid_max_iter", "max_iter 必须为正整数（或 null 表示使用默认值）"
        )
    if max_iter > MAX_ITER_LIMIT:
        raise RequestError(
            "invalid_max_iter",
            f"max_iter={max_iter} 超过硬上限 {MAX_ITER_LIMIT}（本库限定小、中规模）",
        )
    if not isinstance(restart_period, int) or restart_period < 1:
        raise RequestError(
            "invalid_restart_period", "restart_period 必须为 >=1 的整数"
        )
    if not isinstance(stagnation_patience, int) or stagnation_patience < 1:
        raise RequestError(
            "invalid_stagnation_patience", "stagnation_patience 必须为 >=1 的整数"
        )
    if not (0.0 < stagnation_factor < 1.0):
        raise RequestError(
            "invalid_stagnation_factor",
            "stagnation_factor 必须在开区间 (0, 1) 内",
        )
    return float(rtol), float(atol), int(max_iter)


def pcg(
    a: CSRMatrix,
    b: np.ndarray,
    x0: np.ndarray | None = None,
    *,
    rtol: float = DEFAULT_RTOL,
    atol: float = DEFAULT_ATOL,
    max_iter: int | None = None,
    preconditioner: str = "jacobi",
    restart_period: int = DEFAULT_RESTART_PERIOD,
    stagnation_patience: int = DEFAULT_STAG_PATIENCE,
    stagnation_factor: float = DEFAULT_STAG_FACTOR,
) -> PCGResult:
    """对对称稀疏系统 ``A x = b`` 运行预条件共轭梯度。

    本函数不重新检查对称性（由 API 层负责），但矩阵是否正定无法廉价预判，
    因此在迭代中通过瑞利商曲率判据实际检验，非正定时返回诊断状态。

    Args:
        a: 对称 CSR 矩阵（正定由运行时曲率检查保证）。
        b: 右端项，长度 n，所有分量必须有限。
        x0: 初始猜测，长度 n；None 表示零向量。
        rtol / atol: 收敛阈值 ||r|| <= atol + rtol*||b||。
        max_iter: 最大迭代数；None 表示 max(1000, 10n)。
        preconditioner: ``"jacobi"``（对角预条件，要求对角元为正）或
            ``"none"``（不用预条件）。
        restart_period: 周期性真实残差检查/残差替换间隔（迭代数）。
        stagnation_patience / stagnation_factor: 停滞判据参数。

    Returns:
        PCGResult。算法层面的失败不抛异常，而是带诊断信息返回；
        只有输入结构性非法时才抛 :class:`RequestError`。
    """
    n = a.n
    b = np.asarray(b, dtype=np.float64)
    if b.shape != (n,):
        raise RequestError(
            "dimension_mismatch", f"b 长度 {b.shape[0]} 与矩阵阶数 {n} 不一致"
        )
    if not np.all(np.isfinite(b)):
        raise RequestError("b_not_finite", "b 中存在 NaN 或 Inf")
    if x0 is None:
        x = np.zeros(n, dtype=np.float64)
    else:
        x = np.asarray(x0, dtype=np.float64).copy()
        if x.shape != (n,):
            raise RequestError(
                "dimension_mismatch", f"x0 长度 {x.shape[0]} 与矩阵阶数 {n} 不一致"
            )
        if not np.all(np.isfinite(x)):
            raise RequestError("x0_not_finite", "x0 中存在 NaN 或 Inf")
    if preconditioner not in ("none", "jacobi"):
        raise RequestError(
            "invalid_preconditioner",
            f"未知预条件类型 {preconditioner!r}，可选 'none' 或 'jacobi'",
        )
    rtol, atol, max_iter = _validate_options(
        rtol, atol, max_iter, n, restart_period, stagnation_patience, stagnation_factor
    )

    bnorm = float(np.linalg.norm(b))
    threshold = atol + rtol * bnorm

    # 矩阵量级：曲率容差与“对角元过小”判据都需要。
    # hasattr 兼容鸭子类型的算子注入（测试用）。
    if hasattr(a, "data"):
        scale_a = float(np.max(np.abs(a.data))) if a.nnz() > 0 else 1.0
    else:
        diag_probe = a.diagonal()
        scale_a = float(np.max(np.abs(diag_probe))) if diag_probe.size else 1.0
    scale_a = max(1.0, scale_a)

    # 对角预条件：M^{-1} 的作用即逐分量除以对角元。
    inv_diag = None
    if preconditioner == "jacobi":
        diag = a.diagonal()
        nonpos = np.where(diag <= 0.0)[0]
        if nonpos.size > 0:
            k = int(nonpos[0])
            raise RequestError(
                "non_positive_diagonal",
                f"Jacobi 预条件要求所有对角元为正，但 A[{k},{k}]={float(diag[k]):g}",
            )
        tiny = np.finfo(np.float64).tiny * scale_a
        if np.any(diag < tiny):
            k = int(np.argmax(diag < tiny))
            return _fail(
                STATUS_PRECOND_BREAK,
                f"对角预条件失效：A[{k},{k}]={float(diag[k]):.3e} 过小"
                f"（矩阵量级 {scale_a:.1e}），接近下溢范围，无法稳定取逆；"
                "可改用 preconditioner='none'",
                x, 0, float("nan"), float("nan"), float("nan"), [],
            )
        inv_diag = 1.0 / diag

    def apply_m_inv(v: np.ndarray) -> np.ndarray:
        return v * inv_diag if inv_diag is not None else v.copy()

    # ---- 初始残差（真实计算） ----------------------------------------------
    r = b - a.matvec(x)
    rnorm = float(np.linalg.norm(r))
    history = [rnorm]
    best_true = rnorm
    flat_true_checks = 0

    def finish(status: str, message: str, it: int, r_true: np.ndarray) -> PCGResult:
        rn = float(np.linalg.norm(r_true))
        rel = rn / bnorm if bnorm > 0.0 else rn
        return PCGResult(
            status=status,
            message=message,
            x=x,
            iterations=it,
            residual_norm=rn,
            initial_residual_norm=history[0],
            relative_residual=rel,
            residual_history=history,
        )

    if n == 0:
        return finish(STATUS_CONVERGED, "零维系统，空解满足方程", 0, r)
    if not np.isfinite(rnorm):
        return _fail(
            STATUS_DIVERGED, "初始残差非有限（NaN/Inf）", x, 0, rnorm,
            history[0], float("nan"), history,
        )
    if rnorm <= threshold:  # 也覆盖 rnorm == 0（精确解）
        return finish(
            STATUS_CONVERGED,
            f"初始点即满足收敛判据 ||r||={rnorm:.3e} <= {threshold:.3e}",
            0,
            r,
        )

    z = apply_m_inv(r)
    p = z.copy()
    rz = float(np.dot(r, z))
    # rᵀM⁻¹r 对正定 M 必为正；仅把“显著为负/非有限”判作失效，避免在
    # 正常的极小正值（近收敛）上误报。
    if not np.isfinite(rz) or rz < -CURVATURE_TOL * rnorm * float(np.linalg.norm(z)):
        return _finish_breakdown(x, 0, rnorm, bnorm, history, rz)

    def check_curvature(p_vec: np.ndarray, pap: float, it: int):
        """归一化曲率（瑞利商）检查，返回失败 PCGResult 或 None。"""
        pp = float(np.dot(p_vec, p_vec))
        if not np.isfinite(pp) or pp == 0.0:
            return _fail(
                STATUS_ZERO_CURV,
                f"第 {it} 次迭代搜索方向范数为 0，无法继续（矩阵可能奇异）",
                x, it, float(np.linalg.norm(r)), history[0],
                (float(np.linalg.norm(r)) / bnorm if bnorm > 0
                 else float(np.linalg.norm(r))),
                history,
            )
        rq = pap / pp
        if not np.isfinite(rq) or rq < -CURVATURE_TOL * scale_a:
            return _fail_curvature(
                STATUS_NEG_CURV,
                f"第 {it} 次迭代检测到负曲率：瑞利商 pᵀAp/pᵀp={rq:.3e} < 0"
                f"（原始 pᵀAp={pap:.3e}）：矩阵不是正定的（对称但不定），CG 不适用",
                x, it, r, b, bnorm, history,
            )
        if abs(rq) <= CURVATURE_TOL * scale_a:
            return _fail_curvature(
                STATUS_ZERO_CURV,
                f"第 {it} 次迭代检测到零曲率方向：|pᵀAp/pᵀp|={abs(rq):.3e} "
                f"不超过容差 {CURVATURE_TOL:.0e}·||A||="
                f"{CURVATURE_TOL * scale_a:.1e}，矩阵可能奇异（半正定）",
                x, it, r, b, bnorm, history,
            )
        return None

    for k in range(1, max_iter + 1):
        ap = a.matvec(p)
        pap = float(np.dot(p, ap))
        if not np.isfinite(pap):
            return _fail(
                STATUS_DIVERGED,
                f"第 {k} 次迭代 pᵀAp 非有限（NaN/Inf），数值发散",
                x, k, float("nan"), history[0], float("nan"), history,
            )
        curv_fail = check_curvature(p, pap, k)
        if curv_fail is not None:
            return curv_fail

        alpha = rz / pap
        if not np.isfinite(alpha):
            return _fail(
                STATUS_DIVERGED,
                f"第 {k} 次迭代步长非有限（NaN/Inf），数值发散",
                x, k, float("nan"), history[0], float("nan"), history,
            )

        x += alpha * p
        r = r - alpha * ap  # 递推残差
        rnorm_rec = float(np.linalg.norm(r))
        if not np.isfinite(rnorm_rec):
            r_true = b - a.matvec(x)
            return finish(
                STATUS_DIVERGED,
                f"第 {k} 次迭代残差非有限（NaN/Inf），数值发散",
                k,
                r_true,
            )

        # 递推残差到达阈值时，立即重算真实残差确认；周期性检查做监控/替换。
        period_hit = k % restart_period == 0
        near_threshold = rnorm_rec <= threshold
        if period_hit or near_threshold:
            r_true = b - a.matvec(x)
            rtrue_norm = float(np.linalg.norm(r_true))
            if not np.isfinite(rtrue_norm):
                return _fail(
                    STATUS_DIVERGED,
                    f"第 {k} 次迭代真实残差非有限（NaN/Inf），数值发散",
                    x, k, rtrue_norm, history[0], float("nan"), history,
                )

            if near_threshold and rtrue_norm <= threshold:
                history.append(rtrue_norm)
                return finish(
                    STATUS_CONVERGED,
                    f"第 {k} 次迭代收敛：||r||={rtrue_norm:.3e} <= {threshold:.3e}",
                    k,
                    r_true,
                )

            if period_hit:
                history.append(rtrue_norm)

                if rtrue_norm > DIVERGENCE_FACTOR * max(best_true, bnorm, 1.0):
                    return finish(
                        STATUS_DIVERGED,
                        f"第 {k} 次迭代真实残差 {rtrue_norm:.3e} 超过发散阈值"
                        f"（{DIVERGENCE_FACTOR:.0e} × 初始量级）",
                        k,
                        r_true,
                    )

                # 停滞：连续多次真实检查没有实质性下降
                if rtrue_norm < stagnation_factor * best_true:
                    best_true = rtrue_norm
                    flat_true_checks = 0
                else:
                    flat_true_checks += 1
                    if flat_true_checks >= stagnation_patience:
                        return finish(
                            STATUS_STAGNATED,
                            f"第 {k} 次迭代判定停滞：连续 {flat_true_checks} 次"
                            f"真实残差检查（每 {restart_period} 次迭代）相对最好值"
                            f" {best_true:.3e} 下降不足因子 {stagnation_factor:g}，"
                            f"当前 {rtrue_norm:.3e}",
                            k,
                            r_true,
                        )

                # 残差替换：以真实残差校正递推残差，但延续搜索方向与 β 递推，
                # 不整体重启，从而保持共轭性（见模块文档字符串）。
                r = r_true

        z = apply_m_inv(r)
        rz_new = float(np.dot(r, z))
        znorm = float(np.linalg.norm(z))
        if (not np.isfinite(rz_new)
                or rz_new < -CURVATURE_TOL * max(rnorm_rec * znorm, 1e-300)):
            return _finish_breakdown(x, k, rnorm_rec, bnorm, history, rz_new)
        beta = rz_new / rz
        p = z + beta * p
        rz = rz_new

    # ---- 迭代用尽：用真实残差给出最终判定 ----------------------------------
    r_true = b - a.matvec(x)
    rtrue_norm = float(np.linalg.norm(r_true))
    history.append(rtrue_norm)
    if rtrue_norm <= threshold:
        return finish(
            STATUS_CONVERGED,
            f"达到最大迭代数 {max_iter}，最终真实残差 {rtrue_norm:.3e} "
            f"满足判据 <= {threshold:.3e}",
            max_iter,
            r_true,
        )
    return finish(
        STATUS_MAX_ITER,
        f"达到最大迭代数 {max_iter} 仍未收敛：||r||={rtrue_norm:.3e} > "
        f"{threshold:.3e}（atol + rtol*||b||）",
        max_iter,
        r_true,
    )


def _fail(
    status: str,
    message: str,
    x: np.ndarray,
    it: int,
    rnorm: float,
    r0: float,
    rel: float,
    history: list[float],
) -> PCGResult:
    return PCGResult(
        status=status,
        message=message,
        x=x,
        iterations=it,
        residual_norm=float(rnorm),
        initial_residual_norm=float(r0),
        relative_residual=float(rel),
        residual_history=list(history),
    )


def _finish_breakdown(
    x: np.ndarray,
    it: int,
    rnorm: float,
    bnorm: float,
    history: list[float],
    rz: float,
) -> PCGResult:
    return _fail(
        STATUS_PRECOND_BREAK,
        f"第 {it} 次迭代预条件内积 rᵀM⁻¹r={rz:.3e} 显著为负/非有限："
        "预条件子不再正定（Jacobi 对角元异常），无法继续",
        x, it, rnorm, history[0],
        rnorm / bnorm if bnorm > 0 else rnorm, history,
    )


def _fail_curvature(
    status: str,
    message: str,
    x: np.ndarray,
    it: int,
    r_rec: np.ndarray,
    b: np.ndarray,
    bnorm: float,
    history: list[float],
) -> PCGResult:
    """曲率异常退出时组装结果。

    曲率检查发生在本次迭代 ``x += alpha p`` 之前，因此递推残差 r_rec 仍与
    当前 x 对应（相差至多既有舍入误差），直接用它报告残差范数。
    """
    del b  # 仅为签名对称保留
    rn = float(np.linalg.norm(r_rec))
    return PCGResult(
        status=status,
        message=message,
        x=x,
        iterations=it,
        residual_norm=rn,
        initial_residual_norm=history[0],
        relative_residual=rn / bnorm if bnorm > 0 else rn,
        residual_history=list(history),
    )
