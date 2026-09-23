"""JSON 接口层：请求解析、求解编排、结果序列化。

请求示例见 ``examples/request_*.json``。核心算法全部精确；对外只在两个
地方出现十进制小数，且都是**有界**的（区间下界向下截断、上界向上截断），
不会声称未达到的精度。

状态（``status``）：

- ``ok``：成功；
- ``invalid_request``：请求不合法（系数、参数越界等），HTTP/CLI 层对应退出码 2；
- ``isolation_limit``：隔离达到最大深度，存在未分开的根；
- ``refinement_limit``：细化达到最大迭代次数，区间按实际宽度如实给出。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal, getcontext
from fractions import Fraction
from typing import Any

from .isolation import (
    IsolationLimitError,
    RefinementLimitError,
    cauchy_bound,
    isolate_real_roots,
    refine_interval,
    target_width_for_digits,
)
from .polyops import (
    Poly,
    degree,
    evaluate,
    is_zero,
    rational_linear_roots,
    square_free_factorization,
    trim,
)

# Decimal 中间运算给足精度（仅用于显示，精确性由 Fraction 保证）
getcontext().prec = 80

# ---------------------------------------------------------------------------
# 输入范围（明确的适用规模与数值限制）
# ---------------------------------------------------------------------------
MAX_DEGREE = 100
MAX_COEFF_BITS = 4096          # 单个系数分子/分母的最大二进制位数
MAX_DECIMAL_DIGITS = 200
MAX_ITERATIONS = 100_000
MAX_ISOLATION_DEPTH = 100_000
DEFAULT_DECIMAL_DIGITS = 10
DEFAULT_MAX_ITERATIONS = 2000
DEFAULT_MAX_DEPTH = 10_000


class InvalidRequest(ValueError):
    """请求不合法。``code`` 为稳定的机器可读错误码。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# 结果结构
# ---------------------------------------------------------------------------
@dataclass
class Root:
    index: int
    multiplicity: int
    a: Fraction
    b: Fraction
    exact: bool
    a_is_root: bool
    b_is_root: bool
    iterations: int
    factor_degree: int
    factor_index: int
    decimal_digits: int
    target_digits: int
    origin: str = "sturm_bisection"

    def to_dict(self) -> dict[str, Any]:
        dec_lo, dec_hi, width_dec = _decimal_enclosure(self.a, self.b,
                                                       self.target_digits)
        if self.origin == "rational_root_theorem":
            evidence_stmt = (
                "由有理根定理枚举候选 p/q（p 整除常数项、q 整除首项系数），"
                "精确代入命中 p(r)=0，r 为精确有理根，区间退化为点 (r,r)；"
                "其重数由无平方因子分解 gcd(p,p') 给出。"
            )
            method = "rational_root_theorem"
        elif self.exact:
            evidence_stmt = (
                "二分中点精确命中 p(m)=0，m 为精确有理根，区间退化为点 "
                "(m,m)；其重数由无平方因子分解给出。"
            )
            method = "bisection_exact_hit"
        else:
            evidence_stmt = (
                "对无平方因子分量构造 Sturm 序列并二分隔离："
                "V(lower)-V(upper) 对严格开区间 (lower, upper) 恰为 1 "
                "（端点若为根已按 (a,b] 约定扣除），故区间内恰有 1 个"
                "互异实根；重数由无平方因子分解 gcd(p,p') 给出。"
            )
            method = "sturm_bisection"
        if self.exact:
            guarantee_note = (
                "该根为精确有理数，值完全确定，无精度损失；"
                "小数仅为该有理数在指定位数处的有界显示。"
            )
        else:
            guarantee_note = (
                "区间端点为精确有理数；小数仅为有界显示：下界向下截断、"
                "上界向上截断。guaranteed_digits 是实际区间宽度能严格保证"
                "的十进制位数（width <= 10^-d），不夸大未达到的精度。"
            )
        return {
            "index": self.index,
            "multiplicity": self.multiplicity,
            "interval": {
                "lower": f"{self.a}",
                "upper": f"{self.b}",
                "width": f"{self.b - self.a}",
                "width_decimal": width_dec,
                "exact": self.exact,
                "lower_is_root": self.a_is_root,
                "upper_is_root": self.b_is_root,
            },
            "decimal_enclosure": {
                "digits_after_point": self.target_digits,
                "lower_bracket": dec_lo,
                "upper_bracket": dec_hi,
                "guaranteed_digits": self.decimal_digits,
                "note": guarantee_note,
            },
            "refinement_iterations": self.iterations,
            "evidence": {
                "method": method,
                "square_free_factor_index": self.factor_index,
                "factor_degree": self.factor_degree,
                "statement": evidence_stmt,
            },
        }


@dataclass
class RootResult:
    status: str
    kind: str = "polynomial"
    coefficients: list[str] = field(default_factory=list)
    roots: list[Root] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    bounds: dict[str, Any] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "status": self.status,
            "kind": self.kind,
            "polynomial": {
                "coefficients_low_to_high": self.coefficients,
                "degree": degree(
                    [Fraction(s) for s in self.coefficients]
                ) if self.coefficients else -1,
            },
            "summary": self.summary,
            "roots": [r.to_dict() for r in self.roots],
            "search_bounds": self.bounds,
            "parameters": self.params,
        }
        if self.errors:
            d["errors"] = self.errors
        return d


# ---------------------------------------------------------------------------
# 请求解析
# ---------------------------------------------------------------------------
def parse_coefficient(value: Any) -> Fraction:
    """把单个系数解析为精确 Fraction。

    接受：整数；十进制/科学记数法字符串（如 ``"0.1"``、``"1e-3"``、
    ``"1/7"``）；float（按最短往返表示 repr 解析，明确记录这一事实）。
    拒绝：布尔、NaN/Infinity、无法解析的字符串、超限大系数。
    """
    if isinstance(value, bool):
        raise InvalidRequest("bad_coefficient", "布尔值不能作为系数")
    if isinstance(value, int):
        frac = Fraction(value)
    elif isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise InvalidRequest("bad_coefficient", "系数不能是 NaN 或无穷")
        frac = Fraction(repr(value))  # 最短往返，精确表达该二进制浮点值
    elif isinstance(value, str):
        try:
            frac = Fraction(value)
        except (ValueError, ZeroDivisionError):
            raise InvalidRequest(
                "bad_coefficient", f"无法把字符串 {value!r} 解析为有理数"
            )
    else:
        raise InvalidRequest(
            "bad_coefficient", f"不支持的系数类型: {type(value).__name__}"
        )
    if (frac.numerator.bit_length() > MAX_COEFF_BITS
            or frac.denominator.bit_length() > MAX_COEFF_BITS):
        raise InvalidRequest(
            "coefficient_out_of_range",
            f"系数 {value!r} 超出 {MAX_COEFF_BITS} 位二进制限制",
        )
    return frac


def _as_int(value: Any, name: str, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidRequest("bad_parameter", f"{name} 必须是整数")
    if not lo <= value <= hi:
        raise InvalidRequest(
            "parameter_out_of_range",
            f"{name}={value} 超出允许范围 [{lo}, {hi}]",
        )
    return value


def parse_request(req: Any) -> tuple[list[Fraction], int, int, int, Fraction | None]:
    """解析并校验请求，返回 (系数, decimal_digits, max_iter, max_depth, target_width)。"""
    if not isinstance(req, dict):
        raise InvalidRequest("bad_request", "请求必须是 JSON 对象")
    coeffs_raw = req.get("coefficients")
    if not isinstance(coeffs_raw, list):
        raise InvalidRequest(
            "bad_coefficient", "字段 coefficients 必须是数组（低次到高次）"
        )
    coeffs = [parse_coefficient(c) for c in coeffs_raw]

    digits = _as_int(req.get("decimal_digits", DEFAULT_DECIMAL_DIGITS),
                     "decimal_digits", 0, MAX_DECIMAL_DIGITS)
    max_iter = _as_int(req.get("max_refinement_iterations",
                               DEFAULT_MAX_ITERATIONS),
                       "max_refinement_iterations", 1, MAX_ITERATIONS)
    max_depth = _as_int(req.get("max_isolation_depth", DEFAULT_MAX_DEPTH),
                        "max_isolation_depth", 1, MAX_ISOLATION_DEPTH)

    target_width = None
    if "target_width" in req and req["target_width"] is not None:
        target_width = _parse_positive_rational(req["target_width"],
                                                "target_width")
    return coeffs, digits, max_iter, max_depth, target_width


def _parse_positive_rational(value: Any, name: str) -> Fraction:
    try:
        if isinstance(value, bool):
            raise ValueError
        frac = parse_coefficient(value)
    except InvalidRequest:
        raise InvalidRequest("bad_parameter", f"{name} 必须是正有理数")
    if frac <= 0:
        raise InvalidRequest("bad_parameter", f"{name} 必须为正")
    return frac


# ---------------------------------------------------------------------------
# 十进制有界显示
# ---------------------------------------------------------------------------
def _decimal_enclosure(
    a: Fraction, b: Fraction, digits: int
) -> tuple[str, str, str]:
    """返回在 ``digits`` 位小数处对区间的有界十进制显示。

    下界对 a 向下截断、上界对 b 向上截断，保证显示区间覆盖真实区间；
    宽度以科学记数法给出。所有取整方向显式指定，杜绝假精度。
    """
    scale = 10 ** digits
    lo = (a.numerator * scale) // a.denominator          # floor
    hi = -((-b.numerator * scale) // b.denominator)      # ceil

    def fmt(q: int) -> str:
        sign = "-" if q < 0 else ""
        q = abs(q)
        whole, frac_part = divmod(q, scale)
        if digits == 0:
            return f"{sign}{whole}"
        return f"{sign}{whole}.{frac_part:0{digits}d}"

    width = b - a
    width_dec = _scientific(width)
    return fmt(lo), fmt(hi), width_dec


def _scientific(w: Fraction) -> str:
    """把非负 Fraction 宽度表示为 2 位有效数字的科学记数法（十进制）。"""
    if w == 0:
        return "0"
    d = Decimal(w.numerator) / Decimal(w.denominator)
    s = f"{d:.1E}"
    return s.replace("E", "e").replace("e+", "e+").replace("e0", "e")


def guaranteed_decimal_digits(width: Fraction) -> int:
    """区间宽度实际能保证的十进制位数 d：最大的使 width <= 10^-d 的 d。

    与 :func:`rootisolation.isolation.target_width_for_digits` 采用同一
    约定（d 位 ⟺ 宽度 <= 10^-d）。宽度为 0（精确根）由调用方在外层处理。
    """
    if width <= 0:
        return 0
    d = 0
    while Fraction(10) * width <= 1:
        d += 1
        width *= 10
        if d > MAX_DECIMAL_DIGITS + 2:
            break
    return d


# ---------------------------------------------------------------------------
# 主编排
# ---------------------------------------------------------------------------
def solve_poly(req: Any) -> RootResult:
    """解析请求并求解，返回 :class:`RootResult`（永不因算法失败抛异常）。"""
    coeffs, digits, max_iter, max_depth, target_width = parse_request(req)

    params = {
        "decimal_digits_requested": digits,
        "max_refinement_iterations": max_iter,
        "max_isolation_depth": max_depth,
        "target_width": f"{target_width}" if target_width is not None else None,
        "arithmetic": "exact rational (fractions.Fraction); no floating point "
                      "in the core algorithm",
    }

    # 去掉高次零系数
    p = trim(coeffs)

    # ---- 零多项式 --------------------------------------------------------
    if not coeffs or is_zero(p):
        return RootResult(
            status="ok",
            kind="zero_polynomial",
            coefficients=["0"] if not coeffs or is_zero(trim(coeffs)) else [],
            roots=[],
            summary={
                "distinct_real_roots": 0,
                "real_roots_with_multiplicity": 0,
                "degree": -1,
                "root_count_basis": "零多项式在每个实数处为零，实根有无穷多个，"
                                    "不进行隔离；按约定 distinct_real_roots=0。",
                "note": "infinitely many real roots",
            },
            params=params,
        )

    n = degree(p)
    coeff_strs = [f"{c}" for c in p]

    # ---- 常数多项式 ------------------------------------------------------
    if n == 0:
        return RootResult(
            status="ok",
            kind="constant",
            coefficients=coeff_strs,
            roots=[],
            summary={
                "distinct_real_roots": 0,
                "real_roots_with_multiplicity": 0,
                "degree": 0,
                "root_count_basis": f"非零常数 {p[0]} 处处不为零，实根数为 0。",
            },
            params=params,
        )

    if n > MAX_DEGREE:
        raise _invalid_degree(n)

    # ---- 无平方因子分解 + 逐分量隔离 ------------------------------------
    factors = square_free_factorization(p)

    # 一致性自检：各因子次数乘重数之和必须等于次数
    check_degree = sum(degree(g) * m for g, m in factors)
    if check_degree != n:
        raise RuntimeError(f"内部错误：无平方因子次数和 {check_degree} != {n}")

    tw = target_width or target_width_for_digits(digits)

    # (a, b, multiplicity, factor_index, factor_degree) 收集后统一细化
    pending: list[dict[str, Any]] = []
    iso_error: IsolationLimitError | None = None

    for fi, (g, mult) in enumerate(factors):
        fdeg = degree(g)
        # 1) 有理根定理：精确提取有理根（退化点），剩余部分再 Sturm 隔离
        rational_roots, remainder = rational_linear_roots(g)
        for r in rational_roots:
            pending.append({
                "a": r, "b": r,
                "mult": mult, "fi": fi, "fdeg": fdeg,
                "origin": "rational_root_theorem",
            })
        # 2) 剩余多项式（无有理根）做 Sturm + 二分隔离
        if degree(remainder) >= 1:
            try:
                ivs = isolate_real_roots(remainder, max_depth=max_depth)
            except IsolationLimitError as e:
                iso_error = e
                ivs = e.found
            for (a, b) in ivs:
                pending.append({
                    "a": a, "b": b,
                    "mult": mult, "fi": fi, "fdeg": fdeg,
                    "_poly": remainder,
                    "origin": ("bisection_exact_hit" if a == b
                               else "sturm_bisection"),
                })
        if iso_error is not None:
            break

    # 以根的位置排序（按中点、再按左端点）
    pending.sort(key=lambda r: ((r["a"] + r["b"]) / 2, r["a"]))

    roots: list[Root] = []
    refine_failures: list[tuple[Root, RefinementLimitError]] = []
    for i, item in enumerate(pending, start=1):
        a, b = item["a"], item["b"]
        # 非精确根来自 RRT 除尽有理根后的剩余多项式；精确根用原因子即可
        g = item.get("_poly") or _factor_for(factors, item["fi"])
        origin = item.get("origin", "sturm_bisection")
        iterations = 0
        exact = (a == b)
        refine_err: RefinementLimitError | None = None
        if not exact:
            try:
                a, b, iterations = refine_interval(
                    p=g, a=a, b=b, target_width=tw,
                    max_iterations=max_iter,
                )
            except RefinementLimitError as e:
                refine_err = e
            if a == b:
                exact = True
                origin = "bisection_exact_hit"
        # 精确根可保证任意位数；非精确根按实际宽度如实计算保证位数
        gd = digits if exact else guaranteed_decimal_digits(b - a)
        root = Root(
            index=i,
            multiplicity=item["mult"],
            a=a, b=b,
            exact=exact,
            a_is_root=(evaluate(g, a) == 0),
            b_is_root=(evaluate(g, b) == 0),
            iterations=iterations,
            factor_degree=item["fdeg"],
            factor_index=item["fi"],
            decimal_digits=gd,
            target_digits=digits,
            origin=origin,
        )
        roots.append(root)
        if refine_err is not None:
            refine_failures.append((root, refine_err))

    distinct_real = len(roots)
    real_with_mult = sum(r.multiplicity for r in roots)

    # 复根计数：总次数 n，实根占重数之和；其余为非实复根（共轭成对）
    nonreal_with_mult = n - real_with_mult

    bounds = _overall_bounds(factors)

    summary = {
        "degree": n,
        "distinct_real_roots": distinct_real,
        "real_roots_with_multiplicity": real_with_mult,
        "nonreal_complex_roots_with_multiplicity": nonreal_with_mult,
        "all_roots_simple": all(m == 1 for _, m in factors)
                           and all(r.multiplicity == 1 for r in roots),
        "root_count_basis": (
            "对每个无平方因子分量用 Sturm 定理：实根数 = V(-B) - V(B)，"
            "B 为 Cauchy 根界；每个隔离区间满足 V(a)-V(b)=1（开区间内恰一根）。"
            "重数由无平方因子分解 gcd(p,p') 确定。"
        ),
        "square_free_factors": [
            {"index": i, "degree": degree(g), "multiplicity": m}
            for i, (g, m) in enumerate(factors)
        ],
    }

    errors: list[dict[str, Any]] = []
    status = "ok"
    if iso_error is not None:
        status = "isolation_limit"
        errors.append({
            "code": "isolation_limit",
            "message": str(iso_error),
            "max_isolation_depth": iso_error.depth,
            "unresolved_intervals": [
                {"lower": f"{a}", "upper": f"{b}",
                 "estimated_distinct_roots": k}
                for (a, b, k) in iso_error.unresolved
            ],
        })
    if refine_failures:
        if status == "ok":
            status = "refinement_limit"
        worst = max(e.width for _, e in refine_failures)
        errors.append({
            "code": "refinement_limit",
            "message": (
                f"{len(refine_failures)} 个区间达到最大二分迭代次数 "
                f"{max_iter} 仍未窄于目标宽度 {tw}；这些区间按精确端点与"
                "实际宽度如实给出，guaranteed_digits 反映其真实保证位数，"
                "未声称达到请求的精度。"
            ),
            "target_width": f"{tw}",
            "max_refinement_iterations": max_iter,
            "worst_actual_width": f"{worst}",
            "failed_intervals": [
                {"index": r.index,
                 "lower": f"{r.a}", "upper": f"{r.b}",
                 "actual_width": f"{r.b - r.a}",
                 "guaranteed_digits": r.decimal_digits}
                for r, _ in refine_failures
            ],
        })

    return RootResult(
        status=status,
        kind="polynomial",
        coefficients=coeff_strs,
        roots=roots,
        errors=errors,
        summary=summary,
        bounds=bounds,
        params=params,
    )


def _factor_for(factors, i) -> Poly:
    return factors[i][0]


def _overall_bounds(factors) -> dict[str, Any]:
    if not factors:
        return {}
    lo = min(-cauchy_bound(g) for g, _ in factors)
    hi = max(cauchy_bound(g) for g, _ in factors)
    return {
        "lower": f"{lo}",
        "upper": f"{hi}",
        "basis": "各无平方因子分量 Cauchy 根界 B=1+max|a_k/a_n| 的并；"
                 "所有实根严格位于内部，已验证 p(±B) ≠ 0。",
    }


def _invalid_degree(n: int) -> InvalidRequest:
    return InvalidRequest(
        "degree_out_of_range",
        f"多项式次数 {n} 超出本实现限定的 {MAX_DEGREE}（小中规模）",
    )


def solve(req: Any) -> dict[str, Any]:
    """便捷入口：返回可直接 ``json.dumps`` 的字典。

    非法请求抛 :class:`InvalidRequest`（CLI 层负责转成错误 JSON + 退出码 2）。
    """
    return solve_poly(req).to_dict()
