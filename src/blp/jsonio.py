"""JSON 请求/响应接口。

支持两种等价的约束写法（可混用）：

1. 矩阵形式（推荐，与 NumPy 直接对应）::

     {"objective": {"c": [...], "sense": "min", "constant": 0},
      "variables": {"lb": 0, "ub": null},
      "constraints": {"A_ub": [[...]], "b_ub": [...],
                      "A_eq": [[...]], "b_eq": [...]}}

2. 逐行形式，每行 ``{"row": [...], "sense": "<=|>=|=", "rhs": x}``::

     {"objective": {...}, "constraint_rows": [{"row": [1,2], "sense": ">=",
                                               "rhs": 3, "name": "c1"}]}

固定变量用 ``lb[j] == ub[j]`` 表达；变量下界必须非负（非负变量）。
"""

from __future__ import annotations

import math

import numpy as np

from .errors import LPInputError
from .model import (
    MAX_ABS_COEFF, MAX_CONSTRAINTS, MAX_VARIABLES, make_lp,
)
from .solver import SolveResult, solve_lp
from .verify import (
    ray_residuals, solution_residuals, verify_certificate,
)

_ALLOWED_SENSES = {"<=", ">=", "="}
_ALLOWED_OBJ = {"min", "max"}


def _num(v, what: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise LPInputError(f"{what} 必须是数值，收到 {v!r}")
    f = float(v)
    if math.isnan(f):
        raise LPInputError(f"{what} 不能是 NaN")
    return f


def _vec(spec, n, what):
    if isinstance(spec, (int, float)) and not isinstance(spec, bool):
        out = [float(spec)] * n
    else:
        if not isinstance(spec, list):
            raise LPInputError(f"{what} 必须是数组或标量")
        if len(spec) != n:
            raise LPInputError(f"{what} 长度应为 {n}，收到 {len(spec)}")
        out = []
        for k, v in enumerate(spec):
            if v is None:
                out.append(np.inf)
                continue
            if isinstance(v, bool):
                raise LPInputError(f"{what}[{k}] 类型非法")
            out.append(_num(v, f"{what}[{k}]"))
    finite = [v for v in out if np.isfinite(v)]
    if finite and max(abs(v) for v in finite) > MAX_ABS_COEFF:
        raise LPInputError(
            f"{what} 中绝对值超过 {MAX_ABS_COEFF:g}，超出声明的输入范围"
        )
    return out


def parse_request(req: dict) -> dict:
    """解析并校验 JSON 请求，返回构造 LP 所需的数组包。"""
    if not isinstance(req, dict):
        raise LPInputError("请求体必须是 JSON 对象")
    obj = req.get("objective")
    if not isinstance(obj, dict) or "c" not in obj:
        raise LPInputError("缺少 objective.c")
    c_raw = obj["c"]
    if not isinstance(c_raw, list) or not c_raw:
        raise LPInputError("objective.c 必须是非空数组")
    c = [_num(v, f"objective.c[{i}]") for i, v in enumerate(c_raw)]
    if max((abs(v) for v in c), default=0.0) > MAX_ABS_COEFF:
        raise LPInputError(
            f"目标系数绝对值超过 {MAX_ABS_COEFF:g}，超出声明的输入范围"
        )
    n = len(c)
    if n > MAX_VARIABLES:
        raise LPInputError(f"变量数 {n} 超过上限 {MAX_VARIABLES}")
    sense = obj.get("sense", "min")
    if sense not in _ALLOWED_OBJ:
        raise LPInputError(f"objective.sense 只能是 {_ALLOWED_OBJ}")
    c0 = _num(obj.get("constant", 0.0), "objective.constant")

    # ---- 约束 ----
    ub_rows: list[list[float]] = []
    ub_rhs: list[float] = []
    eq_rows: list[list[float]] = []
    eq_rhs: list[float] = []
    row_names: list[str | None] = []  # 仅用于错误信息定位

    cons = req.get("constraints", {})
    if cons and not isinstance(cons, dict):
        raise LPInputError("constraints 必须是对象")
    for key, bkey in (("A_ub", "b_ub"), ("A_eq", "b_eq")):
        A = (cons or {}).get(key)
        b = (cons or {}).get(bkey)
        if A is None:
            if b is not None:
                raise LPInputError(f"constraints 提供了 {bkey} 但没有 {key}")
            continue
        if not isinstance(A, list) or not isinstance(b, list):
            raise LPInputError(f"constraints.{key}/{bkey} 必须是数组")
        if len(A) != len(b):
            raise LPInputError(f"{key} 与 {bkey} 行数不一致")
        for i, row in enumerate(A):
            if not isinstance(row, list) or len(row) != n:
                raise LPInputError(
                    f"{key}[{i}] 必须是长度 {n} 的数组"
                )
            vals = [_num(v, f"{key}[{i}][{j}]") for j, v in enumerate(row)]
            rhs = _num(b[i], f"{bkey}[{i}]")
            if key == "A_ub":
                ub_rows.append(vals)
                ub_rhs.append(rhs)
            else:
                eq_rows.append(vals)
                eq_rhs.append(rhs)

    for i, rc in enumerate(req.get("constraint_rows", []) or []):
        if not isinstance(rc, dict):
            raise LPInputError(f"constraint_rows[{i}] 必须是对象")
        row = rc.get("row")
        if not isinstance(row, list) or len(row) != n:
            raise LPInputError(f"constraint_rows[{i}].row 长度应为 {n}")
        s = rc.get("sense", "<=")
        if s not in _ALLOWED_SENSES:
            raise LPInputError(
                f"constraint_rows[{i}].sense 只能是 {_ALLOWED_SENSES}"
            )
        vals = [_num(v, f"constraint_rows[{i}].row[{j}]")
                for j, v in enumerate(row)]
        rhs = _num(rc.get("rhs", 0.0), f"constraint_rows[{i}].rhs")
        if s == "<=":
            ub_rows.append(vals)
            ub_rhs.append(rhs)
        elif s == ">=":
            ub_rows.append([-v for v in vals])
            ub_rhs.append(-rhs)
        else:
            eq_rows.append(vals)
            eq_rhs.append(rhs)
        row_names.append(rc.get("name"))

    total = len(ub_rhs) + len(eq_rhs)
    if total > MAX_CONSTRAINTS:
        raise LPInputError(f"约束总数 {total} 超过上限 {MAX_CONSTRAINTS}")

    # ---- 变量界 ----
    variables = req.get("variables", {}) or {}
    lb_spec = variables.get("lb", 0.0)
    ub_spec = variables.get("ub", None)
    lb = _vec(lb_spec, n, "variables.lb")
    if any(v < 0 for v in lb):
        raise LPInputError("仅支持非负变量：variables.lb 不能含负数")
    if ub_spec is None:
        ub = [np.inf] * n
    else:
        ub = _vec(ub_spec, n, "variables.ub")

    parsed = {
        "c": c, "sense": sense, "constant": c0,
        "A_ub": ub_rows or None, "b_ub": ub_rhs or None,
        "A_eq": eq_rows or None, "b_eq": eq_rhs or None,
        "lb": lb, "ub": ub,
    }
    for vals in (parsed["A_ub"], parsed["A_eq"]):
        if vals is not None:
            mx = max((abs(x) for row in vals for x in row), default=0.0)
            if mx > MAX_ABS_COEFF:
                raise LPInputError(
                    f"约束系数绝对值超过 {MAX_ABS_COEFF:g}"
                )
    return parsed


def build_lp(parsed: dict):
    """把 parse_request 的结果构造成 model.LP。"""
    return make_lp(
        c=parsed["c"],
        sense=parsed["sense"],
        c0=parsed["constant"],
        A_ub=parsed["A_ub"], b_ub=parsed["b_ub"],
        A_eq=parsed["A_eq"], b_eq=parsed["b_eq"],
        lb=parsed["lb"], ub=parsed["ub"],
    )


def _canonical_arrays(parsed, lp):
    """得到平移后规范系统的数组（供残差核验）。"""
    n = lp.n
    A_le, b_le = None, None
    if parsed["A_ub"] is not None:
        A_le = np.array(parsed["A_ub"], dtype=float)
        b_le = (
            np.array(parsed["b_ub"], dtype=float)
            - A_le @ lp.shift
        )
    A_eq, b_eq = None, None
    if parsed["A_eq"] is not None:
        A_eq = np.array(parsed["A_eq"], dtype=float)
        b_eq = (
            np.array(parsed["b_eq"], dtype=float)
            - A_eq @ lp.shift
        )
    return A_le, b_le, A_eq, b_eq, lp.ub


def run_request(req: dict, pivot_rule: str = "bland") -> dict:
    """解析 → 求解 → 独立残差/证书核验 → 组装 JSON 响应。"""
    parsed = parse_request(req)
    lp = build_lp(parsed)
    result: SolveResult = solve_lp(lp, pivot_rule=pivot_rule)

    A_le, b_le, A_eq, b_eq, eff_ub = _canonical_arrays(parsed, lp)

    if result.status == "optimal":
        y = result.x - lp.shift
        res = solution_residuals(y, A_le, b_le, A_eq, b_eq, eff_ub)
        result.residuals = res
        if not res["feasible"]:
            result.warnings.append(
                f"解未通过独立可行性核验（最大违约 "
                f"{res['overall_max_violation']:.3e}）"
            )
    elif result.status == "unbounded":
        y0 = result.x - lp.shift
        res = ray_residuals(
            y0, result.ray, A_le, b_le, A_eq, b_eq, eff_ub
        )
        result.residuals = res
        if not res["valid"]:
            result.warnings.append("无界射线未通过独立核验")

    if (
        result.status == "infeasible"
        and result.certificate is not None
        and req.get("verify_certificate", True)
    ):
        try:
            chk = verify_certificate(
                result.certificate["rows"],
                A_le, b_le, A_eq, b_eq, eff_ub,
            )
            result.certificate["independent_check"] = chk
        except Exception as exc:  # 核验失败不改变状态，但如实记录
            result.certificate["independent_check"] = {
                "valid": False, "error": str(exc)
            }

    return result.to_dict()
