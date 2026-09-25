"""JSON 接口：字符串/文件 进，JSON 字符串/文件 出。

成功 -> {"ok": true, "result": {...}}
失败 -> {"ok": false, "error": {"code": ..., "message": ...}}
所有 Fraction / numpy 类型在默认序列化器中转为 JSON 原生类型。
"""
from __future__ import annotations

import json
from fractions import Fraction

import numpy as np

from .engine import EngineError, solve


def handle_json(text: str) -> tuple[str, int]:
    """处理一段 JSON 请求文本，返回 (响应文本, 退出码)。"""
    try:
        req = json.loads(text)
    except json.JSONDecodeError as e:
        return _encode(_error("malformed_json", f"请求不是合法 JSON：{e}")), 2
    try:
        result = solve(req)
        return _encode({"ok": True, "result": result}), 0
    except EngineError as e:
        return _encode(_error(e.code, e.message)), 2
    except (RecursionError, MemoryError) as e:
        return _encode(_error("resource_limit",
                              f"超出资源限制：{type(e).__name__}: {e}")), 3


def handle_object(req: dict) -> dict:
    """供 Python 调用方直接使用：返回响应字典，错误以 EngineError 抛出。"""
    return solve(req)


def _error(code, message):
    return {"ok": False, "error": {"code": code, "message": message}}


def _encode(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2,
                      allow_nan=False, default=_default) + "\n"


def _default(o):
    if isinstance(o, Fraction):
        return {"fraction": (f"{o.numerator}/{o.denominator}"
                             if o.denominator != 1 else str(o.numerator))}
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, (np.integer,)):
        return int(o)
    raise TypeError(f"不可序列化的类型：{type(o).__name__}")
