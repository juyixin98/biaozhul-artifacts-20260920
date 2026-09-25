"""自适应数值积分核心算法（不依赖 SciPy）。

提供两条自适应求积路径：

* ``simpson`` —— 自适应复合 Simpson（1/3 公式），Richardson 外推估计误差，
  中点求值在二分细化时复用，每个区间只需新增 2 次求值；
* ``gauss`` —— 基于 Golub-Welsch 过程自行生成的 4 点 / 8 点 Gauss-Legendre
  求积对，以低阶与高阶规则之差作为误差估计。

两者共用相同的误差预算递归分配策略、深度上限、求值次数预算与失败语义：
不收敛、触及深度/预算上限、遇到非有限函数值（NaN/Inf，含端点）时，
:func:`integrate` 返回 ``converged=False`` 并附带机器可读的失败原因，
而不是静默给出一个数值。

本模块面向小中规模问题（默认至多 100 000 次函数求值、递归深度 40）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

# ---- 公开常量：输入范围与默认值（README 与 API 校验共用） -----------------

EPS_MACHINE = 2.220446049250313e-16  # float64 机器精度
DEFAULT_MAX_DEPTH = 20
DEFAULT_MAX_EVALS = 100_000
LIMIT_MAX_DEPTH = 40
LIMIT_MAX_EVALS = 1_000_000
MIN_TOLERANCE = 0.0
MAX_ABS_TOLERANCE = 1.0e6
MAX_REL_TOLERANCE = 1.0
MAX_INTERVAL_SPAN = 1.0e12
MAX_LITERAL_MAG = 1.0e6
MAX_EXPRESSION_CHARS = 200
METHODS = ("simpson", "gauss")


class IntegrationError(Exception):
    """本模块内部使用的失败基类（不直接抛给 JSON 用户）。"""


class NonFiniteSample(IntegrationError):
    """函数求值产生 NaN/Inf。``where`` 为 endpoint/interior/unknown。"""

    def __init__(self, x: float, where: str, detail: str):
        super().__init__(detail)
        self.x = x
        self.where = where
        self.detail = detail


class DepthReached(IntegrationError):
    """在某个区间上达到递归深度上限仍未满足局部误差预算。"""


class BudgetReached(IntegrationError):
    """函数求值次数超过预算。"""


@dataclass
class _Stats:
    max_evals: int
    evals: int = 0
    max_abs_f: float = 0.0
    accepted_intervals: int = 0
    roundoff_hits: int = 0
    deepest: int = 0


@dataclass
class QuadResult:
    """求积结果。失败时 ``value`` 为 ``None``。"""

    converged: bool
    value: float | None
    error_estimate: float | None
    error_code: str | None
    message: str
    n_evals: int
    n_intervals: int
    max_depth: int
    method: str
    details: dict = field(default_factory=dict)


# --------------------------------------------------------------------------
# 函数求值包装：计数、限预算、记录幅值、分类非有限值
# --------------------------------------------------------------------------

def _make_evaluator(f: Callable[[float], float], a: float, b: float,
                    stats: _Stats) -> Callable[[float], float]:
    def eval_f(x: float) -> float:
        if stats.evals >= stats.max_evals:
            raise BudgetReached(
                f"函数求值次数超过预算 {stats.max_evals}；"
                "请提高 max_evals、放宽容差或缩小积分区间。"
            )
        stats.evals += 1
        try:
            y = f(float(x))
        except NonFiniteSample:
            raise
        except Exception as exc:  # 用户函数抛异常视为该点不可求值
            raise NonFiniteSample(
                float(x), _classify_point(x, a, b),
                f"被积函数在 x={float(x):.6g} 处抛出异常: {exc!r}"
            ) from exc
        if isinstance(y, bool) or not isinstance(y, (int, float, np.floating)):
            raise NonFiniteSample(
                float(x), _classify_point(x, a, b),
                f"被积函数在 x={float(x):.6g} 处返回非数值类型: {type(y).__name__}"
            )
        y = float(y)
        if not math.isfinite(y):
            if x == a or x == b:
                where, pos = "endpoint", "区间端点"
            else:
                where, pos = "interior", "区间内部"
            raise NonFiniteSample(
                float(x), where,
                f"被积函数在{pos} x={float(x):.6g} 处产生非有限值 {y!r}"
                "（NaN 或无穷大）。端点处通常意味着端点奇点，例如 1/sqrt(x)；"
                "内部则通常意味着极点。请先做变量代换消去奇点后再积分。"
            )
        mag = abs(y)
        if mag > stats.max_abs_f:
            stats.max_abs_f = mag
        return y

    return eval_f


def _classify_point(x: float, a: float, b: float) -> str:
    return "endpoint" if (x == a or x == b) else "interior"


# --------------------------------------------------------------------------
# 自适应 Simpson（中点复用；每区间细化仅新增 2 次求值）
# --------------------------------------------------------------------------

def _simpson_core(evalf, a, b, whole, f_a, f_b, f_m, eps_abs, eps_rel,
                  depth, max_depth, stats) -> tuple[float, float, float]:
    """返回 (积分值, 局部误差估计, 未被细化时的误差量级)。

    内部区间按 eps_abs/2 平分误差预算（QUADPACK 式）；相对容差按
    局部积分幅值折算为绝对预算。
    """
    if depth > stats.deepest:
        stats.deepest = depth
    m = 0.5 * (a + b)
    h = b - a
    x_lm = a + 0.25 * h
    x_rm = a + 0.75 * h
    f_lm = evalf(x_lm)
    f_rm = evalf(x_rm)
    left = h / 12.0 * (f_a + 4.0 * f_lm + f_m)
    right = h / 12.0 * (f_m + 4.0 * f_rm + f_b)
    refined = left + right
    err15 = abs(refined - whole) / 15.0  # Richardson 误差估计
    value = refined + (refined - whole) / 15.0  # 外推值

    local_tol = max(eps_abs, eps_rel * abs(value))
    # 舍入下限：被积函数幅值接近 1 时，这是 ~1e-14 量级的绝对底
    roundoff_floor = 8.0 * EPS_MACHINE * max(h, 1.0) * stats.max_abs_f

    if err15 <= local_tol or err15 <= roundoff_floor:
        if err15 > local_tol:
            stats.roundoff_hits += 1
        stats.accepted_intervals += 1
        return value, err15, err15

    if depth >= max_depth:
        raise DepthReached(
            f"在区间 [{a:.8g}, {b:.8g}]（宽度 {h:.3g}）上达到深度上限 "
            f"{max_depth}，局部误差估计 {err15:.3e} 仍大于局部预算 "
            f"{local_tol:.3e}。"
        )

    child_eps = 0.5 * eps_abs
    v_l, e_l, _ = _simpson_core(
        evalf, a, m, left, f_a, f_m, f_lm,
        child_eps, eps_rel, depth + 1, max_depth, stats)
    v_r, e_r, _ = _simpson_core(
        evalf, m, b, right, f_m, f_b, f_rm,
        child_eps, eps_rel, depth + 1, max_depth, stats)
    return v_l + v_r, e_l + e_r, err15


# --------------------------------------------------------------------------
# 自适应 Gauss-Legendre（4 点 / 8 点规则对，Golub-Welsch 自行生成节点）
# --------------------------------------------------------------------------

def _gauss_legendre(n: int) -> tuple[np.ndarray, np.ndarray]:
    """Golub-Welsch：用 Legendre 三项递推的 Jacobi 矩阵特征值求 [-1,1] 节点权。"""
    k = np.arange(1, n, dtype=float)
    # Legendre 三项递推 Jacobi 矩阵的次对角元 beta_k = k / sqrt(4k^2-1)
    beta = k / np.sqrt(4.0 * k * k - 1.0)
    jacobi = np.diag(beta, 1) + np.diag(beta, -1)
    nodes, vecs = np.linalg.eigh(jacobi)
    weights = vecs[0, :] ** 2
    weights *= 2.0 / weights.sum()  # 归一化到 [-1,1] 上权值之和为 2
    return nodes, weights


_GL_CACHE: dict[int, tuple[np.ndarray, np.ndarray]] = {}


def _gl(n: int):
    if n not in _GL_CACHE:
        _GL_CACHE[n] = _gauss_legendre(n)
    return _GL_CACHE[n]


def _gauss_core(evalf, a, b, eps_abs, eps_rel, depth, max_depth, stats,
                gl4, w4, gl8, w8):
    if depth > stats.deepest:
        stats.deepest = depth
    h = 0.5 * (b - a)
    c = 0.5 * (a + b)
    s4 = 0.0
    for x_node, w in zip(gl4, w4):
        s4 += w * evalf(c + h * x_node)
    s4 *= h
    s8 = 0.0
    for x_node, w in zip(gl8, w8):
        s8 += w * evalf(c + h * x_node)
    s8 *= h

    err = abs(s8 - s4)  # 保守误差估计（不取 15 分之一）
    value = s8
    local_tol = max(eps_abs, eps_rel * abs(value))
    roundoff_floor = 8.0 * EPS_MACHINE * max(b - a, 1.0) * stats.max_abs_f

    if err <= local_tol or err <= roundoff_floor:
        if err > local_tol:
            stats.roundoff_hits += 1
        stats.accepted_intervals += 1
        return value, err

    if depth >= max_depth:
        raise DepthReached(
            f"在区间 [{a:.8g}, {b:.8g}]（宽度 {b - a:.3g}）上达到深度上限 "
            f"{max_depth}，局部误差估计 {err:.3e} 仍大于局部预算 "
            f"{local_tol:.3e}。"
        )

    m = 0.5 * (a + b)
    child_eps = 0.5 * eps_abs
    v_l, e_l = _gauss_core(
        evalf, a, m, child_eps, eps_rel, depth + 1, max_depth,
        stats, gl4, w4, gl8, w8)
    v_r, e_r = _gauss_core(
        evalf, m, b, child_eps, eps_rel, depth + 1, max_depth,
        stats, gl4, w4, gl8, w8)
    return v_l + v_r, e_l + e_r


# --------------------------------------------------------------------------
# 失败原因的统一组装
# --------------------------------------------------------------------------

def _nonfinite_failure(exc: NonFiniteSample, method: str, stats: _Stats,
                       a: float, b: float) -> QuadResult:
    if exc.where == "endpoint":
        code = "SINGULAR_ENDPOINT"
    else:
        code = "SINGULAR_INTERIOR"
    return QuadResult(
        converged=False, value=None, error_estimate=None,
        error_code=code, message=exc.detail,
        n_evals=stats.evals, n_intervals=stats.accepted_intervals,
        max_depth=stats.deepest, method=method,
        details={"point": exc.x, "position": exc.where,
                 "max_abs_f_observed": stats.max_abs_f,
                 "interval": [a, b]})


def _unresolved_failure(exc: IntegrationError, method: str, stats: _Stats,
                        a: float, b, sign: float, max_depth: int) -> QuadResult:
    if isinstance(exc, BudgetReached):
        code = "EVAL_BUDGET_REACHED"
        hint = f"（已用 {stats.evals} 次求值）"
    else:
        code = "MAX_DEPTH_REACHED"
        hint = ""
    observed = (f"已观测最大函数幅值 |f|={stats.max_abs_f:.3e}。")
    if stats.max_abs_f > 1.0e8:
        suspect = ("最可能原因：未消去的奇点或极窄峰——细化过程中 |f| 很大且误差"
                   "不随宽度线性下降。建议先做变量代换消去端点奇点，或在确认峰位后"
                   "分段积分；也可适度增大 max_depth/max_evals 后重试。")
    else:
        suspect = ("可能原因：高频振荡或极窄峰使规则需要超出允许的细化次数。"
                   "高频振荡可先尝试 method=\"gauss\"（Gauss-Legendre 对振荡更稳健），"
                   "或增大 max_evals / 放宽容差；已知窄峰建议在峰两侧分段积分。")
    msg = f"{exc}{hint}{observed}{suspect}"
    return QuadResult(
        converged=False, value=None, error_estimate=None,
        error_code=code, message=msg,
        n_evals=stats.evals, n_intervals=stats.accepted_intervals,
        max_depth=stats.deepest, method=method,
        details={"interval": [a, b],
                 "max_abs_f_observed": stats.max_abs_f,
                 "max_depth_allowed": max_depth,
                 "max_evals_allowed": stats.max_evals})


# --------------------------------------------------------------------------
# 公开入口
# --------------------------------------------------------------------------

def integrate(f: Callable[[float], float], a: float, b: float,
              eps_abs: float = 1.0e-8, eps_rel: float = 1.0e-8,
              method: str = "simpson",
              max_depth: int = DEFAULT_MAX_DEPTH,
              max_evals: int = DEFAULT_MAX_EVALS) -> QuadResult:
    """对 ``f`` 在 ``[a, b]`` 上做自适应定积分。

    参数范围：
        a, b       有限实数且 |a-b| <= 1e12；a > b 时按定向积分自动反号，
                   a == b 时直接返回 0；
        eps_abs    [0, 1e6]；eps_rel [0, 1]；两者之和必须为正；
        method     "simpson" 或 "gauss"；
        max_depth  [1, 40]，默认 20（最小区间宽度约 |b-a|/2^max_depth）；
        max_evals  [1, 1_000_000]，默认 100_000。

    失败时返回的 :class:`QuadResult` 中 ``converged=False``、``value=None``，
    error_code 取值：
        MAX_DEPTH_REACHED / EVAL_BUDGET_REACHED —— 不收敛；
        SINGULAR_ENDPOINT / SINGULAR_INTERIOR   —— 非有限函数值（含奇点）。
    """
    if method not in METHODS:
        raise ValueError(f"method 必须是 {METHODS} 之一，得到 {method!r}")
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool)
               and math.isfinite(float(v)) for v in (a, b)):
        raise ValueError("a、b 必须为有限实数")
    if abs(b - a) > MAX_INTERVAL_SPAN:
        raise ValueError(f"区间跨度不得超过 {MAX_INTERVAL_SPAN:g}")
    if not isinstance(eps_abs, (int, float)) or isinstance(eps_abs, bool) \
            or not 0.0 <= float(eps_abs) <= MAX_ABS_TOLERANCE:
        raise ValueError("eps_abs 必须在 [0, 1e6] 内")
    if not isinstance(eps_rel, (int, float)) or isinstance(eps_rel, bool) \
            or not 0.0 <= float(eps_rel) <= MAX_REL_TOLERANCE:
        raise ValueError("eps_rel 必须在 [0, 1] 内")
    if eps_abs + eps_rel <= 0:
        raise ValueError("eps_abs 与 eps_rel 之和必须为正")
    if not isinstance(max_depth, int) or isinstance(max_depth, bool) \
            or not 1 <= max_depth <= LIMIT_MAX_DEPTH:
        raise ValueError(f"max_depth 必须是 [1, {LIMIT_MAX_DEPTH}] 内的整数")
    if not isinstance(max_evals, int) or isinstance(max_evals, bool) \
            or not 1 <= max_evals <= LIMIT_MAX_EVALS:
        raise ValueError(f"max_evals 必须是 [1, {LIMIT_MAX_EVALS}] 内的整数")

    if a == b:
        return QuadResult(True, 0.0, 0.0, None, "退化区间 [a,a]，积分恒为 0。",
                          0, 1, 0, method, {"interval": [a, b]})

    sign = 1.0
    lo, hi = a, b
    if a > b:  # 定向积分：换到 [b,a] 再反号
        lo, hi, sign = b, a, -1.0

    stats = _Stats(max_evals=max_evals)
    evalf = _make_evaluator(f, lo, hi, stats)

    try:
        if method == "simpson":
            # Simpson 第一层：5 个点，中点将在细化时复用
            f_a = evalf(lo)
            f_b = evalf(hi)
            m = 0.5 * (lo + hi)
            f_m = evalf(m)
            whole = (hi - lo) / 6.0 * (f_a + 4.0 * f_m + f_b)
            value, err, _ = _simpson_core(
                evalf, lo, hi, whole, f_a, f_b, f_m,
                float(eps_abs), float(eps_rel), 1, max_depth, stats)
        else:
            gl4, w4 = _gl(4)
            gl8, w8 = _gl(8)
            value, err = _gauss_core(
                evalf, lo, hi, float(eps_abs), float(eps_rel),
                1, max_depth, stats, gl4, w4, gl8, w8)

        if not math.isfinite(value):
            raise NonFiniteSample(
                float("nan"), "unknown",
                "加权求和产生非有限结果：被积函数幅值过大导致溢出，"
                "请先对被积函数做尺度变换或分段积分。")

        # 顶层严格验收：所有子区间的局部误差估计之和仍需满足总预算
        top_tol = float(eps_abs) + float(eps_rel) * abs(value)
        if err > top_tol and stats.roundoff_hits == 0:
            # 理论上递归保证不会走到这里；保留为诚实的兜底
            return QuadResult(
                False, None, None, "FAILED_TOLERANCE",
                f"细化终止但总误差估计 {err:.3e} 超过总预算 {top_tol:.3e}。",
                stats.evals, stats.accepted_intervals, stats.deepest, method,
                details={"interval": [lo, hi], "error_estimate": err,
                         "tolerance": top_tol})

        note = None
        if stats.roundoff_hits:
            note = (f"有 {stats.roundoff_hits} 个区间因到达舍入误差下限而接受，"
                    "误差估计在该量级下不再可靠（请求的容差可能已低于机器精度"
                    "所能保证的范围）。")
        return QuadResult(
            True, sign * value, sign * err, None,
            note or "积分在给定容差与深度/预算限制内收敛。",
            stats.evals, stats.accepted_intervals, stats.deepest, method,
            details={"interval": [a, b], "roundoff_hits": stats.roundoff_hits})

    except NonFiniteSample as exc:
        res = _nonfinite_failure(exc, method, stats, lo, hi)
        return res
    except (DepthReached, BudgetReached) as exc:
        return _unresolved_failure(exc, method, stats, lo, hi, sign, max_depth)
