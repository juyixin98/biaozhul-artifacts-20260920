"""数值输入校验与规范化（数据契约的第一道边界）。

规则：
1. 系数只接受数、[实, 虚] 二元数组或 {real, imag} 对象；拒绝字符串、布尔、NaN/Inf。
2. 零首项（最高次零系数）剥离规范化；全部为零（零多项式）拒绝。
3. 规范化为首一多项式（除以首项），降低系数尺度对求根的影响。
4. 次数上限由资源策略拒绝（resource_exhausted，区别于输入格式错误）。
"""

from __future__ import annotations

import math
from typing import Any, Sequence

import numpy as np

from .errors import (
    InvalidCoefficientError,
    NonFiniteCoefficientError,
    ResourceExhaustedError,
    ZeroPolynomialError,
)
from .models import CoeffOrder

_COMPLEX_TYPES = (complex, np.complexfloating)
_REAL_TYPES = (int, float, np.integer, np.floating)


def _to_complex(item: Any, position: int) -> complex:
    if isinstance(item, bool):
        raise InvalidCoefficientError(
            f"位置 {position} 的系数是布尔值，需为数值", {"position": position}
        )
    if isinstance(item, _COMPLEX_TYPES):
        value = complex(item)
    elif isinstance(item, _REAL_TYPES):
        value = complex(float(item), 0.0)
    elif isinstance(item, Sequence) and not isinstance(item, (str, bytes)):
        if len(item) != 2:
            raise InvalidCoefficientError(
                f"位置 {position} 的数组系数必须恰有 [实部, 虚部] 两个元素",
                {"position": position, "length": len(item)},
            )
        value = complex(_real_part(item[0], position, "real"),
                        _real_part(item[1], position, "imag"))
    elif isinstance(item, dict):
        if set(item) - {"real", "imag"}:
            raise InvalidCoefficientError(
                f"位置 {position} 的对象系数只允许 real/imag 字段",
                {"position": position, "keys": sorted(item)},
            )
        value = complex(
            _real_part(item.get("real", 0.0), position, "real"),
            _real_part(item.get("imag", 0.0), position, "imag"),
        )
    else:
        raise InvalidCoefficientError(
            f"位置 {position} 的系数类型不支持: {type(item).__name__}",
            {"position": position, "type": type(item).__name__},
        )
    if not (math.isfinite(value.real) and math.isfinite(value.imag)):
        raise NonFiniteCoefficientError(
            f"位置 {position} 的系数非有限值（NaN/Inf 不允许）",
            {"position": position, "real": value.real, "imag": value.imag},
        )
    return value


def _real_part(item: Any, position: int, part: str) -> float:
    if isinstance(item, bool) or not isinstance(item, _REAL_TYPES):
        raise InvalidCoefficientError(
            f"位置 {position} 的 {part} 部分必须是实数",
            {"position": position, "part": part, "type": type(item).__name__},
        )
    val = float(item)
    if not math.isfinite(val):
        raise NonFiniteCoefficientError(
            f"位置 {position} 的 {part} 部分非有限值", {"position": position}
        )
    return val


def parse_and_normalize(
    raw: Sequence[Any],
    order: CoeffOrder,
    max_degree: int,
) -> tuple[np.ndarray, int]:
    """返回 (首一化升序系数 numpy 数组, 剥离的零首项个数)。"""
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise InvalidCoefficientError("coefficients 必须是数组", {"type": type(raw).__name__})
    if len(raw) == 0:
        raise InvalidCoefficientError("coefficients 不能为空数组（至少 1 个系数）")

    coeffs = [_to_complex(c, i) for i, c in enumerate(raw)]
    arr = np.asarray(coeffs, dtype=np.complex128)

    if order is CoeffOrder.DESCENDING:
        # numpy.roots 风格：index 0 为最高次；最高次零系数在数组头部
        arr = arr[::-1]

    # 剥离零首项（升序表示下的高端零）。用相对尺度判零，抵御下溢尺度。
    scale = float(np.max(np.abs(arr)))
    zero_tol = 1e-15 * scale if scale > 0.0 else 0.0
    stripped = 0
    while arr.size > 0 and abs(arr[-1]) <= zero_tol:
        arr = arr[:-1]
        stripped += 1

    if arr.size == 0:
        raise ZeroPolynomialError(
            "所有系数均为零：零多项式的根没有定义，拒绝求解",
            {"coefficient_count": len(coeffs)},
        )

    degree = arr.size - 1
    if degree > max_degree:
        raise ResourceExhaustedError(
            f"多项式次数 {degree} 超过资源上限 {max_degree}",
            {"degree": degree, "max_degree": max_degree},
        )

    # 首一化
    arr = arr / arr[-1]
    return arr, stripped
