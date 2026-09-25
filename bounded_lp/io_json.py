"""JSON 请求解析与响应组装。

请求样例（矩阵式，推荐）::

    {
      "sense": "min",
      "c": [-3, -5],
      "A_ub": [[1, 0], [0, 2], [3, 2]],
      "b_ub": [4, 12, 18],
      "lb": [0, 0],
      "ub": [null, 10],
      "options": {"rule": "bland"}
    }

也支持约束列表式（不能与 A_ub/A_eq 同时出现）::

    {
      "c": [-3, -5],
      "constraints": [
        {"a": [1, 0], "op": "<=", "b": 4},
        {"a": [1, 1], "op": "=",  "b": 4}
      ]
    }

响应固定包含顶层 ``status``，取值：
``optimal`` / ``infeasible`` / ``unbounded`` / ``failed`` / ``invalid_request``。
求解得到结论时附带残差（残差为绝对量）。
"""

from __future__ import annotations

import json
import math
from typing import Any

import numpy as np

from bounded_lp.errors import InvalidProblem
from bounded_lp.problem import LPProblem
from bounded_lp.simplex import SolveResult, solve
from bounded_lp.tolerance import (
    DEFAULT_TOLERANCE,
    MAX_CONSTRAINTS,
    MAX_ITERATIONS,
    MAX_VARIABLES,
    Tolerance,
)

_VALID_OPS = {"<=", "=<", "<", ">=", "=>", ">", "=", "=="}


class InvalidRequest(Exception):
    """请求无法解析为合法 LP；errors 为带字段路径的错误列表。"""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


# --------------------------------------------------------------------------
# 请求解析
# --------------------------------------------------------------------------


def parse_request(data: Any) -> tuple[LPProblem, dict]:
    """把已解析的 JSON 对象转为 :class:`LPProblem` 与选项。"""
    errors: list[str] = []
    if not isinstance(data, dict):
        raise InvalidRequest(["请求根必须是 JSON 对象"])

    n = _detect_n(data, errors)

    c = _read_numeric_vector(data.get("c"), "c", n, errors) if n is not None else None
    sense = str(data.get("sense", "min"))
    if sense not in ("min", "max"):
        errors.append("sense 只能是 'min' 或 'max'")

    lb = _read_numeric_vector(data.get("lb", [0.0] * (n or 0)), "lb", n,
                              errors, allow_none=False) if n is not None else None
    ub = _read_numeric_vector(data.get("ub"), "ub", n,
                              errors, allow_none=True, optional=True)

    A_ub, b_ub, A_eq, b_eq = _read_constraints(data, n, errors)

    if errors:
        raise InvalidRequest(errors)

    try:
        problem = LPProblem(
            c=c, sense=sense,
            A_ub=A_ub, b_ub=b_ub, A_eq=A_eq, b_eq=b_eq,
            lb=lb, ub=ub,
        )
    except InvalidProblem as exc:
        raise InvalidRequest([str(exc)]) from exc

    options = _read_options(data.get("options", {}))
    return problem, options


def _detect_n(data, errors):
    c = data.get("c")
    if c is None:
        errors.append("缺少字段 c（目标系数向量）")
        return None
    if not isinstance(c, list) or not c:
        errors.append("c 必须是非空数值数组")
        return None
    if not all(_is_number(v) for v in c):
        errors.append("c 的每个元素必须是数值（不能含 null/字符串）")
        return None
    return len(c)


def _read_numeric_vector(value, path, n, errors, *, allow_none=False, optional=False):
    if value is None:
        if optional:
            return None
        value = [0.0] * n
    if not isinstance(value, list) or len(value) != n:
        errors.append(f"{path} 必须是长度 {n} 的数组")
        return None
    out = []
    for i, v in enumerate(value):
        if v is None:
            if allow_none:
                out.append(math.inf)
                continue
            errors.append(f"{path}[{i}] 不能为 null（仅 ub 允许 null 表示 +∞）")
            return None
        if not _is_number(v):
            errors.append(f"{path}[{i}] 必须是数值")
            return None
        out.append(float(v))
    return out


def _read_matrix(value, path, n, errors):
    if not isinstance(value, list) or not all(isinstance(r, list) for r in value):
        errors.append(f"{path} 必须是二维数组（行向量的数组）")
        return None
    A = []
    for i, row in enumerate(value):
        if len(row) != n or not all(_is_number(v) for v in row):
            errors.append(f"{path}[{i}] 必须是长度 {n} 的数值数组")
            return None
        A.append([float(v) for v in row])
    return A


def _read_constraints(data, n, errors):
    has_matrix = "A_ub" in data or "b_ub" in data or "A_eq" in data or "b_eq" in data
    has_list = "constraints" in data
    if has_matrix and has_list:
        errors.append("constraints 列表不能与 A_ub/A_eq 矩阵形式同时使用")
        return None, None, None, None

    if has_list:
        return _read_constraint_list(data["constraints"], n, errors)

    A_ub = b_ub = A_eq = b_eq = None
    if "A_ub" in data or "b_ub" in data:
        A_ub = _read_matrix(data.get("A_ub", []), "A_ub", n, errors)
        b_ub = _read_numeric_vector(
            data.get("b_ub"), "b_ub",
            len(A_ub) if A_ub is not None else 0,
            errors, allow_none=False)
    if "A_eq" in data or "b_eq" in data:
        A_eq = _read_matrix(data.get("A_eq", []), "A_eq", n, errors)
        b_eq = _read_numeric_vector(
            data.get("b_eq"), "b_eq",
            len(A_eq) if A_eq is not None else 0,
            errors, allow_none=False)
    return A_ub, b_ub, A_eq, b_eq


def _read_constraint_list(raw, n, errors):
    if not isinstance(raw, list):
        errors.append("constraints 必须是对象数组")
        return None, None, None, None
    if len(raw) > MAX_CONSTRAINTS:
        errors.append(f"约束数 {len(raw)} 超过上限 {MAX_CONSTRAINTS}")
    ub_rows, ub_rhs, eq_rows, eq_rhs = [], [], [], []
    for i, item in enumerate(raw):
        if not isinstance(item, dict) or "a" not in item or "op" not in item or "b" not in item:
            errors.append(f"constraints[{i}] 需要字段 a（系数）、op、b（右端）")
            continue
        a = item["a"]
        if not isinstance(a, list) or len(a) != n or not all(_is_number(v) for v in a):
            errors.append(f"constraints[{i}].a 必须是长度 {n} 的数值数组")
            continue
        op = item["op"]
        if not isinstance(op, str) or op not in _VALID_OPS:
            errors.append(f"constraints[{i}].op={op!r} 非法，支持 <= >= =")
            continue
        if not _is_number(item["b"]):
            errors.append(f"constraints[{i}].b 必须是数值")
            continue
        a_f = [float(v) for v in a]
        b_f = float(item["b"])
        if op in ("=", "=="):
            eq_rows.append(a_f)
            eq_rhs.append(b_f)
        elif op in ("<=", "=<", "<"):
            ub_rows.append(a_f)
            ub_rhs.append(b_f)
        else:  # >= 族
            ub_rows.append([-v for v in a_f])
            ub_rhs.append(-b_f)
    if errors:
        return None, None, None, None
    return (
        ub_rows or None, ub_rhs or None,
        eq_rows or None, eq_rhs or None,
    )


def _read_options(raw) -> dict:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise InvalidRequest(["options 必须是对象"])
    out: dict = {}
    rule = raw.get("rule", "bland")
    if rule not in ("bland", "dantzig"):
        raise InvalidRequest(["options.rule 只能是 'bland' 或 'dantzig'"])
    out["rule"] = rule

    max_iter = raw.get("max_iterations", MAX_ITERATIONS)
    if not isinstance(max_iter, int) or isinstance(max_iter, bool) or not (1 <= max_iter <= 10_000_000):
        raise InvalidRequest(["options.max_iterations 必须是 1..10_000_000 的整数"])
    out["max_iterations"] = max_iter

    tol_kwargs = {}
    for name in ("feas_tol", "pivot_tol", "reduced_tol"):
        if name in raw:
            v = raw[name]
            if not _is_number(v) or not (0.0 < float(v) < 1.0):
                raise InvalidRequest([f"options.{name} 必须是 (0,1) 内的数值"])
            tol_kwargs[name] = float(v)
    if tol_kwargs:
        try:
            out["tol"] = Tolerance(**tol_kwargs)
        except ValueError as exc:
            raise InvalidRequest([str(exc)]) from exc
    else:
        out["tol"] = DEFAULT_TOLERANCE
    return out


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) \
        and not (isinstance(v, float) and (math.isnan(v) or math.isinf(v)))


# --------------------------------------------------------------------------
# 顶层处理与响应
# --------------------------------------------------------------------------


def handle_request(data: Any) -> dict:
    """处理一个已解析的 JSON 请求，返回可 json.dumps 的响应字典。"""
    try:
        problem, options = parse_request(data)
    except InvalidRequest as exc:
        return {
            "status": "invalid_request",
            "errors": exc.errors,
            "limits": {
                "max_variables": MAX_VARIABLES,
                "max_constraints": MAX_CONSTRAINTS,
            },
        }

    result: SolveResult = solve(
        problem,
        rule=options["rule"],
        max_iterations=options["max_iterations"],
        tol=options["tol"],
    )
    response = result.to_dict()
    response["n_variables"] = problem.n
    response["n_constraints"] = problem.A_ub.shape[0] + problem.A_eq.shape[0]
    return response


def loads(s: str) -> dict:
    """解析 JSON 字符串并处理；JSON 语法错误也返回标准响应。"""
    try:
        data = json.loads(s, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        return {
            "status": "invalid_request",
            "errors": [f"JSON 语法错误：{exc.msg}（行 {exc.lineno} 列 {exc.colno}）"],
        }
    except ValueError as exc:
        # parse_constant 拒绝 NaN / Infinity
        return {
            "status": "invalid_request",
            "errors": [f"{exc}（标准 JSON 不允许 NaN/Infinity）"],
        }
    return handle_request(data)


def _reject_constant(value: str):
    # 拒绝 NaN / Infinity（标准 JSON 不允许）
    raise ValueError(f"非法 JSON 常量 {value}")
