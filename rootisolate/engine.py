"""编排层：JSON 风格字典请求 -> 字典响应。

流程（全程精确有理数）：
  1. 校验并解析系数 / 搜索范围 / 容差 / 深度（拒绝越界与非有限浮点）；
  2. 常数与零多项式走专门分支（零多项式没有“根”的定义，显式标记）；
  3. Yun 无平方分解 f = c·∏ p_i^i（根的重数即因子指标）；
  4. 对每个无平方因子构造 Sturm 链，用变号数二分隔离 + 细化；
  5. 用 Sturm 变号数（而不是几何猜测）筛选 [A,B] 内的根；
  6. 汇总区间、点根、计数依据与失败状态；十进制只用于展示并附严格误差界。
"""
from __future__ import annotations

from fractions import Fraction

import numpy as np

from . import decimalize, isolate, limits, polynomial as P, squarefree, sturm


class EngineError(Exception):
    """输入/规模类错误。code 为机器可读错误码，message 为人类可读说明。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------

def _parse_rational(value, field: str) -> Fraction:
    if isinstance(value, bool):
        raise EngineError("invalid_input", f"{field} 不能是布尔值")
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, float):
        raise EngineError(
            "invalid_input",
            f"{field} 不接受浮点字面量（JSON number 会丢失精确性）；"
            f"请用整数或字符串，如 \"1/3\"、\"-0.25\"、\"1e-6\"")
    if isinstance(value, str):
        s = value.strip()
        try:
            return Fraction(s)
        except (ValueError, ZeroDivisionError):
            raise EngineError("invalid_input",
                              f"{field} 无法解析为有理数：{value!r}")
    raise EngineError("invalid_input",
                      f"{field} 必须是整数或有理数字符串，收到 {type(value).__name__}")


def _check_bits(q: Fraction, field: str):
    if max(abs(q.numerator.bit_length()), q.denominator.bit_length()) > limits.MAX_COEFF_BITS:
        raise EngineError(
            "input_out_of_range",
            f"{field} 的分子/分母位数超过 {limits.MAX_COEFF_BITS} 位上限")


def _parse_coefficients(req: dict):
    coeffs = req.get("coefficients")
    if coeffs is None:
        raise EngineError("invalid_request", "缺少 coefficients 字段")
    if not isinstance(coeffs, list) or not coeffs:
        raise EngineError("invalid_input",
                          "coefficients 必须是非空数组（空数组请改用零多项式标记）")
    order = req.get("coefficient_order", "ascending")
    if order not in ("ascending", "descending"):
        raise EngineError("invalid_input",
                          "coefficient_order 只能是 ascending 或 descending")
    qs = [_parse_rational(c, f"coefficients[{i}]") for i, c in enumerate(coeffs)]
    if order == "descending":
        qs.reverse()
    for i, q in enumerate(qs):
        _check_bits(q, f"coefficients[{i}]")
    return qs, order


def _parse_eps(req: dict) -> Fraction:
    eps = req.get("epsilon", "1e-12")
    if not isinstance(eps, (str, int)) and not isinstance(eps, bool):
        raise EngineError("invalid_input", "epsilon 必须是有理数字符串或整数")
    if isinstance(eps, bool):
        raise EngineError("invalid_input", "epsilon 不能是布尔值")
    q = _parse_rational(eps, "epsilon")
    if q <= 0:
        raise EngineError("invalid_input", "epsilon 必须为正有理数")
    if q < limits.EPS_MIN:
        raise EngineError(
            "epsilon_out_of_range",
            f"epsilon={q} 小于 {limits.EPS_MIN}：超出本服务承诺的精度，拒绝假精度")
    return q


def _parse_depth(req: dict) -> int:
    d = req.get("max_depth", limits.MAX_DEPTH_DEFAULT)
    if isinstance(d, bool) or not isinstance(d, int):
        raise EngineError("invalid_input", "max_depth 必须是正整数")
    if not (1 <= d <= limits.MAX_DEPTH_LIMIT):
        raise EngineError("input_out_of_range",
                          f"max_depth 必须在 [1, {limits.MAX_DEPTH_LIMIT}]")
    return d


def _parse_bounds(req, coeffs, default_bound):
    if "bounds" in req and req["bounds"] is not None:
        b = req["bounds"]
        if not isinstance(b, dict):
            raise EngineError("invalid_input", "bounds 必须是含 lower/upper 的对象")
        lo = _parse_rational(b.get("lower", -default_bound), "bounds.lower")
        hi = _parse_rational(b.get("upper", default_bound), "bounds.upper")
        custom = True
    else:
        lo, hi = -default_bound, default_bound
        custom = False
    if lo >= hi:
        raise EngineError("invalid_input", f"bounds 必须满足 lower < upper：[{lo}, {hi}]")
    return lo, hi, custom


# ---------------------------------------------------------------------------
# 输出工具
# ---------------------------------------------------------------------------

def _rational(q: Fraction) -> dict:
    q = Fraction(q)
    return {"fraction": f"{q.numerator}/{q.denominator}" if q.denominator != 1
            else str(q.numerator),
            "numerator": q.numerator, "denominator": q.denominator}


def _coeffs_out(a: np.ndarray) -> list:
    return [_rational(Fraction(c)) for c in a]


def _interval_out(a: Fraction, b: Fraction) -> dict:
    return {"lower": _rational(a), "upper": _rational(b),
            "width": _rational(b - a)}


def _decimal_out(info: dict) -> dict:
    return {
        "midpoint": info["midpoint_decimal"],
        "radius": info["radius_decimal"],
        "decimal_places": info["decimal_places"],
        "certificate": info["error_bound_note"],
        "midpoint_rational": _rational(info["midpoint"]),
        "radius_rational": _rational(info["radius_exact"]),
    }


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def solve(req: dict) -> dict:
    if not isinstance(req, dict):
        raise EngineError("invalid_request", "请求必须是 JSON 对象")

    raw_coeffs, order = _parse_coefficients(req)
    eps = _parse_eps(req)
    max_depth = _parse_depth(req)

    f = P.poly(raw_coeffs)
    degree = P.degree(f)

    # --- 零多项式 ---------------------------------------------------------
    if P.is_zero(f):
        resp = _base_response(req, order, f, degree, eps, max_depth)
        resp.update({
            "polynomial_kind": "zero",
            "status": "zero_polynomial",
            "message": "零多项式在每一点都为零，“根”无定义，不输出区间或根数。",
            "real_roots": [],
            "root_count_distinct": None,
            "root_count_with_multiplicity": None,
            "counting_basis": None,
        })
        return resp

    # --- 非零常数 ---------------------------------------------------------
    if degree == 0:
        resp = _base_response(req, order, f, degree, eps, max_depth)
        resp.update({
            "polynomial_kind": "constant",
            "status": "ok",
            "real_roots": [],
            "root_count_distinct": 0,
            "root_count_with_multiplicity": 0,
            "counting_basis": {
                "method": "constant polynomial evaluation",
                "note": "非零常数 c 恒不为零，实根数为 0（无需 Sturm 链）。",
                "value": _rational(f[0]),
            },
        })
        return resp

    if degree > limits.MAX_DEGREE:
        raise EngineError("degree_out_of_range",
                          f"次数 {degree} 超过上限 {limits.MAX_DEGREE}")

    # --- 无平方分解 -------------------------------------------------------
    factors, leading = squarefree.squarefree(f)
    if not factors:  # 理论上 degree>=1 不会发生
        raise EngineError("internal_error", "无平方分解未返回任何因子")

    bound = P.cauchy_bound(f)
    lo, hi, custom_bounds = _parse_bounds(req, f, bound)

    # 全局搜索范围固定为 Cauchy 界；[lo,hi] 只用于筛选
    A, B = -bound, bound
    if P.sign_at(f, A) == 0 or P.sign_at(f, B) == 0:
        raise EngineError("internal_error", "Cauchy 界端点不应为根")

    resp = _base_response(req, order, f, degree, eps, max_depth)
    resp["search_region"] = {
        "cauchy_bounds": _interval_out(A, B),
        "requested_bounds": _interval_out(lo, hi),
        "note": "隔离在严格的 Cauchy 根界内完成；请求区间仅用于筛选输出。",
    }

    roots = []
    unresolved_global = []
    refine_limited = False
    factor_reports = []
    global_distinct = 0
    global_weighted = 0

    for fp, mult in factors:
        chain = sturm.sturm_chain(fp)
        leaves, unresolved = isolate.isolate_factor(
            chain, fp, A, B, max_depth)

        va = sturm.v_at(chain, A)
        vb = sturm.v_at(chain, B)
        distinct_factor = va - vb
        # 交叉校验：每个隔离叶（含点根）各代表一个根；未决区间按其内部根数计
        leaf_count = sum(1 for L_ in leaves)
        unresolved_count = sum(L_["interior_count"] for L_ in unresolved)
        assert leaf_count + unresolved_count == distinct_factor, \
            f"factor count mismatch {leaf_count}+{unresolved_count}!={distinct_factor}"

        # 请求边界恰好是本因子的精确根（边界点根单独精确表示）
        boundary_points = set()
        for t in (lo, hi):
            if P.sign_at(fp, t) == 0:
                boundary_points.add(t)

        for L_ in unresolved:
            ua, ub = L_["a"], L_["b"]
            # 标注未决区间与请求闭区间 [lo,hi] 的位置关系
            if ub < lo or ua > hi:
                overlap = "outside_requested_bounds"
            elif ua >= lo and ub <= hi:
                overlap = "inside_requested_bounds"
            else:
                overlap = "crosses_requested_bound"
            unresolved_global.append(
                {"factor_multiplicity": mult, "interval": _interval_out(ua, ub),
                 "var_left": L_["va"], "var_right": L_["vb"],
                 "distinct_roots_in_interval": L_["interior_count"],
                 "left_is_point_root": L_["left_is_point_root"],
                 "right_is_point_root": L_["right_is_point_root"],
                 "relation_to_requested_bounds": overlap,
                 "depth": L_["depth"]})

        for L_ in leaves:
            relation, exact_t = _classify(chain, fp, L_, lo, hi, boundary_points)
            if relation == "outside":
                continue
            if exact_t is not None:
                # 用精确边界点替换其所在孤立叶，避免重复计数
                rec, status = ({"kind": "point", "r": exact_t,
                                "depth": L_.get("depth", 0)}, "exact")
            else:
                rec, status = isolate.refine_leaf(chain, fp, L_, eps, max_depth)
                if status == "refine_depth":
                    refine_limited = True
            entry = _build_root(rec, mult, chain, fp, relation, status, eps)
            roots.append(entry)

        # 全局总数以 Sturm 证书为准（含未决区间代表的根）
        global_distinct += distinct_factor
        global_weighted += distinct_factor * mult

        factor_reports.append({
            "factor": {
                "coefficients_ascending": _coeffs_out(fp),
                "monic": True,
            },
            "multiplicity": mult,
            "sturm_chain_length": len(chain),
            "sturm_sequence": [_coeffs_out(s) for s in chain],
            "variations_at_cauchy_lower": va,
            "variations_at_cauchy_upper": vb,
            "distinct_real_roots": distinct_factor,
            "weighted_real_roots": distinct_factor * mult,
            "unresolved_intervals": sum(1 for _ in unresolved),
        })

    roots.sort(key=lambda e: e["_sort"])
    for e in roots:
        del e["_sort"]

    requested_distinct = sum(1 for e in roots)
    requested_weighted = sum(e["multiplicity"] for e in roots)

    if unresolved_global:
        status = "isolation_depth"
        message = (f"隔离触及 max_depth={max_depth}：仍有 "
                   f"{sum(u['distinct_roots_in_interval'] for u in unresolved_global)} "
                   f"个不同根未能彼此分开；已给区间与变号数可信，但未输出对应根。")
    elif refine_limited:
        status = "refine_depth"
        message = (f"所有根已隔离，但部分区间未能在 max_depth={max_depth} 内细化到 "
                   f"epsilon={eps}；这些区间的计数与边界仍严格成立，宽度如实给出。")
    else:
        status = "ok"
        message = "全部实根完成隔离与细化，区间边界与计数均由 Sturm 变号数严格保证。"

    resp.update({
        "polynomial_kind": "non_constant",
        "status": status,
        "message": message,
        "real_roots": roots,
        "root_count_distinct": requested_distinct,
        "root_count_with_multiplicity": requested_weighted,
        "global_root_count_distinct": global_distinct,
        "global_root_count_with_multiplicity": global_weighted,
        "square_free_factorization": {
            "leading_constant": _rational(leading),
            "factors": [{"coefficients_ascending": _coeffs_out(fp),
                         "multiplicity": mult} for fp, mult in factors],
            "algorithm": "Yun's algorithm over Q[x], exact rational arithmetic",
        },
        "counting_basis": {
            "method": "Sturm's theorem with sign variations",
            "theorem": "对无平方因子 p，开区间 (a,b) 内不同实根数 = V(a)-V(b)；"
                       "重根按其无平方因子指标计重数。",
            "arithmetic": "fractions.Fraction 精确有理数；NumPy object 数组仅做容器",
            "epsilon": _rational(eps),
            "factor_reports": factor_reports,
        },
        "unresolved": unresolved_global,
        "warnings": _warnings(req, custom_bounds),
    })
    return resp


# ---------------------------------------------------------------------------
# 叶子分类与结果构造
# ---------------------------------------------------------------------------

def _classify(chain, fp, leaf, lo, hi, boundary_points):
    """判定叶子相对请求闭区间 [lo,hi] 的归属。

    返回 (relation, exact_t)：
      relation ∈ inside / outside / boundary_exact；
      exact_t 非 None 时，表示该叶对应的根已被证明恰为请求边界点 lo/hi，
      调用方应改用精确点根 {t} 输出（避免区间与边界含糊）。
    所有归属都由精确求值 / Sturm 变号数决定，不做几何猜测。
    """
    if leaf["kind"] == "point":
        r = leaf["r"]
        if r < lo or r > hi:
            return "outside", None
        return ("boundary_exact" if r in boundary_points else "inside"), (
            r if r in boundary_points else None)

    a, b = leaf["a"], leaf["b"]
    if b <= lo or a >= hi:
        return "outside", None
    if a >= lo and b <= hi:
        return "inside", None

    # 区间跨过 lo/hi：若边界点本身就是该因子的根且落在叶内，
    # 因叶只含一个根，它必为该叶的根，按精确边界点输出
    for t in sorted(boundary_points):
        if a < t < b:
            return "boundary_exact", t

    # 否则用 Sturm 变号数精确判断唯一的根在 [lo,hi] 交集的内侧还是外侧
    if _locate_crossing_root(chain, leaf, a, b, lo, hi) == "inside":
        return "inside", None
    return "outside", None


def _locate_crossing_root(chain, leaf, a, b, lo, hi):
    """已知 (a,b) 恰含一个无平方单根、两端非根，且 (a,b) 跨过 lo/hi。

    用 Sturm 变号数精确判断该根是否落在与请求闭区间的交集内。
    边界点若是根本身，已在 _classify 中先行处理，故这里 lo_/hi_ 均非根，
    (lo_, hi_] 内根数 = V(lo_)-V(hi_)，恰为 1 当且仅当唯一根在内。
    """
    lo_ = max(a, lo)
    hi_ = min(b, hi)
    v_l = leaf["va"] if lo_ == a else sturm.v_at(chain, lo_)
    v_r = leaf["vb"] if hi_ == b else sturm.v_at(chain, hi_)
    return "inside" if v_l - v_r == 1 else "outside"


def _build_root(rec, mult, chain, fp, relation, refine_status, eps) -> dict:
    if rec["kind"] == "point":
        r = rec["r"]
        info = decimalize.decimalize_point(r, eps)
        return {
            "kind": "exact",
            "multiplicity": mult,
            "relation_to_requested_bounds": relation,
            "exact_value": _rational(r),
            "interval": _interval_out(r, r),
            "interval_width": _rational(Fraction(0)),
            "sign_at_endpoints": {"lower": 0, "upper": 0},
            "approximation": _decimal_out(info),
            "evidence": {
                "source": "bisection midpoint evaluated to zero (exact rational root)",
                "isolation_depth": rec["depth"],
                "refinement_status": "exact",
            },
            "_sort": (0, r, r),
        }

    a, b = rec["a"], rec["b"]
    info = decimalize.decimalize_interval(a, b)
    # 严格校验宣传的误差界确实覆盖（实现自检，防止假精度）
    assert abs((a + b) / 2 - info["midpoint"]) + (b - a) / 2 <= info["radius_exact"] + 0
    va, vb = rec["va"], rec["vb"]
    return {
        "kind": "isolated_interval",
        "multiplicity": mult,
        "relation_to_requested_bounds": relation,
        "interval": _interval_out(a, b),
        "interval_width": _rational(b - a),
        "sign_at_endpoints": {
            "lower": _sign_word(P.sign_at(fp, a)),
            "upper": _sign_word(P.sign_at(fp, b)),
        },
        "approximation": _decimal_out(info),
        "evidence": {
            "variations_left": va,
            "variations_right": vb,
            "variation_difference": va - vb,
            "count_assertion": "V(lower)-V(upper)=1 => 恰有一个不同实根",
            "isolation_depth": rec.get("isolation_depth", rec["depth"]),
            "refinement_steps": rec.get("refinement_steps", 0),
            "total_depth": rec["depth"],
            "achieved_width": _rational(rec.get("width", b - a)),
            "refinement_status": refine_status,
        },
        "_sort": (1, a, b),
    }


def _sign_word(v) -> str:
    return "positive" if v > 0 else ("negative" if v < 0 else "zero")


def _warnings(req, custom_bounds):
    w = []
    if isinstance(req.get("epsilon"), str) and ("." in req["epsilon"] or "e" in req["epsilon"].lower()):
        w.append("epsilon 已按十进制字符串精确解析为有理数，未引入浮点。")
    return w


def _base_response(req, order, f, degree, eps, max_depth) -> dict:
    return {
        "status": None,
        "input": {
            "coefficients": [_rational(c) for c in f] if not P.is_zero(f) else [],
            "coefficient_order": order,
            "degree": degree,
            "epsilon": _rational(eps),
            "max_depth": max_depth,
            "is_zero_polynomial": P.is_zero(f),
        },
    }
