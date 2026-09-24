"""周期角距离等角度工具。

关键约定：关节角是周期量（J6 尤其明显，范围 ±2π），
角度间距离必须使用环绕（wrap-around）距离，不能直接做普通差值。
"""

from __future__ import annotations

import numpy as np

TWO_PI = 2.0 * np.pi


def wrap_to_pi(angle: float | np.ndarray) -> float | np.ndarray:
    """把角（或角数组）包裹到 (-π, π]。"""
    a = np.asarray(angle, dtype=float)
    wrapped = (a + np.pi) % TWO_PI - np.pi
    # 上面的 % 会把 -π 映射为 -π；规范到 (-π, π]
    wrapped = np.where(wrapped == -np.pi, np.pi, wrapped)
    if np.isscalar(angle) or np.ndim(angle) == 0:
        return float(wrapped)
    return wrapped


def angular_distance(a: float | np.ndarray, b: float | np.ndarray) -> float | np.ndarray:
    """两个（或两组）关节角之间的周期距离，结果落在 [0, π]。

    angular_distance(a, b) == angular_distance(a, b + 2π) 恒成立，
    这是与 `a - b` 普通差值的本质区别。
    """
    return np.abs(wrap_to_pi(np.asarray(a, dtype=float) - np.asarray(b, dtype=float)))


def nearest_equivalent_in_range(
    value: float, low: float, high: float, reference: float | None = None
) -> float | None:
    """在 [low, high] 内寻找与 value 相差 2π 整数倍的代表角。

    用于：迭代解可能落在限位表示区间之外（例如 J6 算出 7.0 rad，
    而限位为 [-2π, 2π]），先把解平移进区间，再谈是否违反限位。
    区间宽度 ≥ 2π（如 J6 的 ±2π）时可能存在两个合法代表角，
    此时用 reference（通常是当前关节角）选周期距离最近的一个；
    找不到等价角（区间宽度不足一个周期的关节）时返回 None。
    """
    if high < low:
        raise ValueError("high 必须不小于 low")
    options: list[float] = []
    k = int(np.floor((low - value) / TWO_PI))
    while True:
        cand = value + k * TWO_PI
        if cand > high + 1e-12:
            break
        if cand >= low - 1e-12:
            options.append(float(min(max(cand, low), high)))
        k += 1
    if not options:
        return None
    # 注意：同角的 2π 代表对任意参考点的“周期距离”恒等，无法区分，
    # 因此这里必须用**原始数值差**（非包裹）挑选代表：
    # 无 reference 时选绝对值最小的主值；有 reference 时选数值上最近的，
    # 例如参考 q6=5.5 时保留 5.52 而非 -0.76，避免无谓整圈回转。
    anchor = 0.0 if reference is None else float(reference)
    return min(options, key=lambda c: abs(c - anchor))


def canonicalize_to_limits(
    q: np.ndarray,
    low: np.ndarray,
    high: np.ndarray,
    reference: np.ndarray | None = None,
) -> np.ndarray | None:
    """把整组关节角逐轴平移进限位区间；任一轴无等价代表角则返回 None。

    区间宽度 ≥ 2π 的轴若存在多个代表角，按 reference 选周期最近者。
    """
    out = np.zeros_like(q, dtype=float)
    for i, v in enumerate(q):
        ref = None if reference is None else float(reference[i])
        c = nearest_equivalent_in_range(float(v), float(low[i]), float(high[i]), ref)
        if c is None:
            return None
        out[i] = c
    return out
