"""JSON 接口：请求解析与响应序列化。

请求格式见 ``examples/request_example.json`` 与 README。要点：

* 量测步为 ``null`` 表示该步完全无量测；向量中的 ``null`` 表示该分量缺测
  （解析为 NaN 后进入滤波器）。
* 所有输出数字保证有限，序列化使用 ``allow_nan=False`` 的严格 JSON。
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from . import config
from .errors import KalmanError
from .model import LinearKalmanModel
from .runner import BatchResult, run_batch

SCHEMA_VERSION = 1


class JsonRequestError(KalmanError):
    """JSON 请求本身格式不合法。"""

    code = "invalid_request"


def process_request(payload: Any) -> dict:
    """处理一个已解析的 JSON 请求对象，返回可 JSON 序列化的响应字典。

    模型/初值层面的致命错误抛出 :class:`KalmanError`，由 CLI/Web 层包装成
    错误信封；单步数值错误进入 ``steps[*].status == "error"``。
    """
    if not isinstance(payload, dict):
        raise JsonRequestError("请求根必须是 JSON 对象")

    model = _build_model(payload.get("model"))
    init = payload.get("initial_state")
    if not isinstance(init, dict):
        raise JsonRequestError("缺少 initial_state 对象")
    x0 = np.asarray(_numeric_list(init.get("x"), "initial_state.x"), dtype=np.float64)
    P0 = np.asarray(_numeric_matrix(init.get("P"), "initial_state.P"), dtype=np.float64)

    measurements = _parse_measurements(payload.get("measurements"), model.meas_dim)
    masks = _parse_masks(payload.get("masks"), model.meas_dim, len(measurements))
    controls = _parse_controls(payload.get("controls"), len(measurements))

    options = payload.get("options") or {}
    if not isinstance(options, dict):
        raise JsonRequestError("options 必须是对象")

    result = run_batch(
        model, x0, P0,
        measurements=measurements,
        masks=masks,
        controls=controls,
    )

    return _build_response(result, options)


# ---------------------------------------------------------------------- #
def _build_model(mp) -> LinearKalmanModel:
    if not isinstance(mp, dict):
        raise JsonRequestError("缺少 model 对象")
    try:
        F = _numeric_matrix(mp.get("F"), "model.F")
        H = _numeric_matrix(mp.get("H"), "model.H")
        Q = _numeric_matrix(mp.get("Q"), "model.Q")
        R = _numeric_matrix(mp.get("R"), "model.R")
        B = (
            _numeric_matrix(mp.get("B"), "model.B")
            if mp.get("B") is not None
            else None
        )
        return LinearKalmanModel(F=F, H=H, Q=Q, R=R, B=B)
    except KalmanError:
        raise
    except (TypeError, ValueError) as exc:
        raise JsonRequestError(f"model 解析失败：{exc}") from exc


def _numeric_list(value, path: str) -> list[float]:
    if not isinstance(value, list) or not value:
        raise JsonRequestError(f"{path} 必须是非空数组")
    out: list[float] = []
    for i, v in enumerate(value):
        out.append(_scalar(v, f"{path}[{i}]"))
    return out


def _numeric_matrix(value, path: str) -> list[list[float]]:
    if not isinstance(value, list) or not value:
        raise JsonRequestError(f"{path} 必须是非空二维数组")
    rows = []
    width = None
    for i, row in enumerate(value):
        if not isinstance(row, list) or not row:
            raise JsonRequestError(f"{path}[{i}] 必须是非空数组")
        if width is None:
            width = len(row)
        elif len(row) != width:
            raise JsonRequestError(f"{path} 行长度不一致：第 {i} 行为 {len(row)}，应为 {width}")
        rows.append([_scalar(v, f"{path}[{i}][{j}]") for j, v in enumerate(row)])
    return rows


def _scalar(v, path: str) -> float:
    # JSON 中布尔是 int 的子类，明确拒绝以免维度参数被误写
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise JsonRequestError(f"{path} 必须是数字")
    fv = float(v)
    if math.isnan(fv) or math.isinf(fv):
        raise JsonRequestError(f"{path} 不能是 NaN/Infinity（缺测量测请用 null）")
    if abs(fv) > config.MAX_ABS_VALUE:
        raise JsonRequestError(
            f"{path} 的绝对值超过上限 {config.MAX_ABS_VALUE:g}"
        )
    return fv


def _parse_measurements(value, m: int):
    if not isinstance(value, list) or not value:
        raise JsonRequestError("measurements 必须是非空数组")
    parsed = []
    for k, step in enumerate(value):
        if step is None:
            parsed.append(None)
            continue
        if not isinstance(step, list) or len(step) != m:
            raise JsonRequestError(
                f"measurements[{k}] 必须是长度 {m} 的数组或 null"
            )
        vec = np.empty(m, dtype=np.float64)
        for i, v in enumerate(step):
            if v is None:
                vec[i] = np.nan
            else:
                vec[i] = _scalar(v, f"measurements[{k}][{i}]")
        parsed.append(vec)
    return parsed


def _parse_masks(value, m: int, t: int):
    if value is None:
        return None
    if not isinstance(value, list) or len(value) != t:
        raise JsonRequestError(f"masks 必须是长度 {t} 的数组")
    out = []
    for k, step in enumerate(value):
        if not isinstance(step, list) or len(step) != m:
            raise JsonRequestError(f"masks[{k}] 必须是长度 {m} 的布尔数组")
        row = []
        for i, v in enumerate(step):
            if not isinstance(v, bool):
                raise JsonRequestError(f"masks[{k}][{i}] 必须是 true/false")
            row.append(v)
        out.append(np.asarray(row, dtype=bool))
    return out


def _parse_controls(value, t: int):
    if value is None:
        return None
    if not isinstance(value, list) or len(value) != t:
        raise JsonRequestError(f"controls 必须是长度 {t} 的数组")
    out = []
    for k, step in enumerate(value):
        if not isinstance(step, list):
            raise JsonRequestError(f"controls[{k}] 必须是数组")
        out.append(np.asarray(
            [_scalar(v, f"controls[{k}][{i}]") for i, v in enumerate(step)],
            dtype=np.float64,
        ))
    return out


# ---------------------------------------------------------------------- #
def _build_response(result: BatchResult, options: dict) -> dict:
    include_P = bool(options.get("return_covariance", True))
    include_diag = bool(options.get("diagnostics", True))

    steps = []
    n_updated = n_pred = n_err = 0
    max_asym = 0.0
    min_eig = math.inf

    for rec in result.records:
        entry: dict[str, Any] = {
            "index": rec.index,
            "status": rec.status,
            "available": rec.available,
        }
        if rec.innovation is not None:
            entry["innovation"] = rec.innovation
        if rec.status == "error":
            n_err += 1
            entry["error"] = {
                "code": rec.error_code,
                "message": rec.error_message,
            }
        if rec.x is not None:
            entry["x"] = [float(v) for v in rec.x]
            if include_P and rec.P is not None:
                entry["P"] = _matrix_to_list(rec.P)
            if include_diag and rec.P is not None:
                asym = float(np.max(np.abs(rec.P - rec.P.T)))
                eig0 = float(np.linalg.eigvalsh(rec.P)[0])
                entry["diagnostics"] = {
                    "max_asymmetry": asym,
                    "min_eigenvalue": eig0,
                }
                max_asym = max(max_asym, asym)
                min_eig = min(min_eig, eig0)
        if rec.status == "updated":
            n_updated += 1
        elif rec.status == "predicted_only":
            n_pred += 1
        steps.append(entry)

    last_ok = next(
        (r for r in reversed(result.records) if r.x is not None), None
    )
    final = None
    if last_ok is not None:
        final = {"index": last_ok.index, "x": [float(v) for v in last_ok.x]}
        if include_P:
            final["P"] = _matrix_to_list(last_ok.P)

    return {
        "ok": result.ok,
        "schema_version": SCHEMA_VERSION,
        "summary": {
            "num_steps": len(result.records),
            "num_updated": n_updated,
            "num_predicted_only": n_pred,
            "num_errors": n_err,
            "max_covariance_asymmetry": (max_asym if steps else 0.0),
            "min_covariance_eigenvalue": (min_eig if min_eig != math.inf else None),
        },
        "final": final,
        "steps": steps,
    }


def _matrix_to_list(mat: np.ndarray) -> list[list[float]]:
    return [[float(v) for v in row] for row in np.asarray(mat)]


def error_envelope(exc: Exception) -> dict:
    """把致命异常包装成标准错误信封。"""
    return {
        "ok": False,
        "error": {
            "code": getattr(exc, "code", "kalman_error"),
            "message": str(exc),
        },
    }
