"""JSON 请求/响应接口层。

请求结构（详见 README）::

    {
      "matrix": {"n": int, "data": [...], "indices": [...], "indptr": [...]},
      "b": [...],
      "x0": [...] | null,            # 可选
      "rtol": 1e-8, "atol": 1e-12,   # 可选
      "max_iter": int | null,        # 可选
      "preconditioner": "jacobi"     # 可选，"jacobi" 或 "none"
    }

成功响应::

    {"ok": true, "status": "converged", ...}

非法输入响应（HTTP 无关，纯库函数）::

    {"ok": false, "error": {"code": "...", "message": "..."}}

算法层面的失败（不收敛、非正曲率等）仍然 ``ok=true``——请求本身合法，
求解结果的成败看 ``status``/``converged``。
"""

from __future__ import annotations

from typing import Any

import numpy as np

from .csr import CSRMatrix, MAX_NNZ, check_symmetric
from .errors import RequestError
from .pcg import (
    DEFAULT_ATOL,
    DEFAULT_RTOL,
    FAILING_STATUSES,
    pcg,
)

MAX_DIM = 10_000
SYMMETRY_TOL = 1e-9

_ALLOWED_KEYS = {
    "matrix",
    "b",
    "x0",
    "rtol",
    "atol",
    "max_iter",
    "preconditioner",
}
_MATRIX_KEYS = {"n", "data", "indices", "indptr"}


def _is_real_number(v: Any) -> bool:
    """JSON 数字且非布尔；bool 是 int 的子类，显式排除。"""
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _finite_vector(v: Any, name: str, length: int) -> np.ndarray:
    if not isinstance(v, list):
        raise RequestError(f"{name}_not_array", f"{name} 必须是 JSON 数组")
    if len(v) != length:
        raise RequestError(
            f"{name}_length_mismatch",
            f"{name} 长度({len(v)})必须与矩阵阶数 n={length} 一致",
        )
    out = np.empty(length, dtype=np.float64)
    for i, item in enumerate(v):
        if not _is_real_number(item):
            raise RequestError(
                f"{name}_not_numeric",
                f"{name}[{i}] 不是有限数值（类型 {type(item).__name__}）",
            )
        out[i] = float(item)
    if not np.all(np.isfinite(out)):
        raise RequestError(
            f"{name}_not_finite", f"{name} 中存在 NaN 或 Infinity（JSON 标准也不支持）"
        )
    return out


def _build_matrix(mat: Any) -> CSRMatrix:
    if not isinstance(mat, dict):
        raise RequestError("matrix_not_object", "matrix 必须是 JSON 对象")
    missing = _MATRIX_KEYS - mat.keys()
    if missing:
        raise RequestError(
            "matrix_missing_fields", f"matrix 缺少字段：{sorted(missing)}"
        )

    n = mat["n"]
    if not isinstance(n, int) or isinstance(n, bool):
        raise RequestError("n_not_integer", "matrix.n 必须是整数")
    if n < 0:
        raise RequestError("n_negative", f"matrix.n 必须非负，实际 n={n}")
    if n > MAX_DIM:
        raise RequestError(
            "n_too_large", f"矩阵阶数 n={n} 超过上限 {MAX_DIM}（限定小、中规模）"
        )

    for key in ("data", "indices", "indptr"):
        if not isinstance(mat[key], list):
            raise RequestError(f"{key}_not_array", f"matrix.{key} 必须是 JSON 数组")
    if len(mat["data"]) != len(mat["indices"]):
        raise RequestError(
            "indices_length_mismatch",
            f"matrix.data({len(mat['data'])}) 与 matrix.indices"
            f"({len(mat['indices'])}) 长度必须一致",
        )
    if len(mat["indptr"]) != n + 1:
        raise RequestError(
            "indptr_length_mismatch",
            f"n={n} 时 matrix.indptr 长度必须为 {n + 1}，实际为 {len(mat['indptr'])}",
        )
    if len(mat["data"]) > MAX_NNZ:
        raise RequestError(
            "too_many_nonzeros",
            f"非零元素数 {len(mat['data'])} 超过上限 {MAX_NNZ}",
        )

    # data：必须全部是有限数值
    data = np.empty(len(mat["data"]), dtype=np.float64)
    for i, item in enumerate(mat["data"]):
        if not _is_real_number(item):
            raise RequestError(
                "data_not_numeric", f"matrix.data[{i}] 不是数值（类型 {type(item).__name__}）"
            )
        data[i] = float(item)
    if not np.all(np.isfinite(data)):
        raise RequestError("data_not_finite", "matrix.data 中存在 NaN 或 Infinity")

    # indices / indptr：必须全部是整数（拒绝浮点，即使是 1.0 这种）
    indices = np.empty(len(mat["indices"]), dtype=np.intp)
    for i, item in enumerate(mat["indices"]):
        if not isinstance(item, int) or isinstance(item, bool):
            raise RequestError(
                "indices_not_integer",
                f"matrix.indices[{i}] 必须是整数，实际为 {item!r}",
            )
        indices[i] = item
    indptr = np.empty(n + 1, dtype=np.intp)
    for i, item in enumerate(mat["indptr"]):
        if not isinstance(item, int) or isinstance(item, bool):
            raise RequestError(
                "indptr_not_integer",
                f"matrix.indptr[{i}] 必须是整数，实际为 {item!r}",
            )
        indptr[i] = item

    # 结构校验全部集中在 CSRMatrix 构造器中完成
    return CSRMatrix(data, indices, indptr, n)


def _check_spd_prerequisites(a: CSRMatrix) -> None:
    """对称正定的廉价前置检查。

    * 对称性：结构 + 数值（容差 1e-9 相对误差）；
    * 对角元存在且为正：SPD 的必要条件。
    充分性（是否正定）无法廉价验证，留给 PCG 的运行时曲率检查。
    """
    check_symmetric(a, tol=SYMMETRY_TOL)
    diag = a.diagonal()
    missing = [i for i in range(a.n) if not _has_diagonal(a, i)]
    if missing:
        i = missing[0]
        raise RequestError(
            "diagonal_missing",
            f"第 {i} 行缺少对角元 A[{i},{i}]；对称正定矩阵必须有正对角元",
        )
    nonpos = np.where(diag <= 0.0)[0]
    if nonpos.size > 0:
        i = int(nonpos[0])
        raise RequestError(
            "non_positive_diagonal",
            f"A[{i},{i}]={float(diag[i]):g}：正定矩阵的对角元必须严格为正",
        )


def _has_diagonal(a: CSRMatrix, i: int) -> bool:
    lo, hi = int(a.indptr[i]), int(a.indptr[i + 1])
    seg = a.indices[lo:hi]
    p = int(np.searchsorted(seg, i))
    return p < hi - lo and int(seg[p]) == i


def solve_request(payload: Any) -> dict[str, Any]:
    """处理一个已解析的 JSON 请求（dict），返回可 json.dumps 的响应 dict。

    非法输入不抛异常，统一返回 ``{"ok": False, "error": {...}}``；
    合法请求的任何算法结果（含失败状态）都返回 ``{"ok": True, ...}``。
    """
    if not isinstance(payload, dict):
        return _error_response("request_not_object", "请求体必须是 JSON 对象")

    unknown = set(payload) - _ALLOWED_KEYS
    if unknown:
        return _error_response(
            "unknown_fields", f"存在不支持的字段：{sorted(unknown)}"
        )
    if "matrix" not in payload:
        return _error_response("missing_matrix", "缺少必填字段 matrix")
    if "b" not in payload:
        return _error_response("missing_b", "缺少必填字段 b")

    try:
        a = _build_matrix(payload["matrix"])
        n = a.n
        b = _finite_vector(payload["b"], "b", n)
        x0 = None
        if "x0" in payload and payload["x0"] is not None:
            x0 = _finite_vector(payload["x0"], "x0", n)

        rtol, atol = DEFAULT_RTOL, DEFAULT_ATOL
        if "rtol" in payload:
            if not _is_real_number(payload["rtol"]):
                raise RequestError("invalid_rtol", "rtol 必须是非负数值")
            rtol = float(payload["rtol"])
        if "atol" in payload:
            if not _is_real_number(payload["atol"]):
                raise RequestError("invalid_atol", "atol 必须是非负数值")
            atol = float(payload["atol"])

        max_iter = None
        if "max_iter" in payload and payload["max_iter"] is not None:
            mv = payload["max_iter"]
            if not isinstance(mv, int) or isinstance(mv, bool):
                raise RequestError("invalid_max_iter", "max_iter 必须是正整数或 null")
            max_iter = mv

        preconditioner = "jacobi"
        if "preconditioner" in payload:
            preconditioner = payload["preconditioner"]
            if preconditioner not in ("jacobi", "none"):
                raise RequestError(
                    "invalid_preconditioner",
                    "preconditioner 只支持 'jacobi' 或 'none'",
                )

        _check_spd_prerequisites(a)
        result = pcg(
            a,
            b,
            x0,
            rtol=rtol,
            atol=atol,
            max_iter=max_iter,
            preconditioner=preconditioner,
        )
    except RequestError as exc:
        return _error_response(exc.code, exc.message)

    body = result.to_dict()
    body["ok"] = True
    body["algorithm_failed"] = result.status in FAILING_STATUSES
    body["nnz"] = a.nnz()
    body["n"] = n
    return body


def _error_response(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}
