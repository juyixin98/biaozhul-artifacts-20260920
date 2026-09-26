"""实系数二次方程解析求根（禁止离散采样的核心保障）。

求解 a*t^2 + b*t + c = 0 的全部实根，使用稳定求根公式
（Numerical Recipes 5.6 节的 q 公式），避免 -b ± sqrt(D) 在
|b|≈sqrt(D) 时抵消丢精度。
"""

from __future__ import annotations

import math

# 判别式相对容限：D 与 max(b^2, |4ac|) 同量级比较
DISCRIMINANT_TOL = 1e-12


def solve_quadratic(a: float, b: float, c: float, tol: float = DISCRIMINANT_TOL):
    """返回升序排列的实根元组。

    - ()      : 无实根（严格分离）
    - (t,)     : 一个二重实根（相切 / 擦边）
    - (t1, t2) : 两个不同实根，t1 < t2

    要求 a != 0（一次方程不属于本模块职责）。
    """
    if a == 0.0:
        raise ValueError("solve_quadratic 要求 a != 0")

    disc = b * b - 4.0 * a * c
    scale = max(b * b, abs(4.0 * a * c))
    # 负判别式且超出舍入噪声范围：确无实根
    if disc < -tol * max(scale, 1e-300):
        return ()
    # 相切附近：把微小负判别式夹到 0，输出一个二重根
    if disc <= tol * max(scale, 1e-300):
        return (-b / (2.0 * a),)

    sqrt_disc = math.sqrt(disc)
    if b == 0.0:
        q = -0.5 * sqrt_disc
    else:
        q = -0.5 * (b + math.copysign(sqrt_disc, b))
    # q 不可能为 0（b=0 已单独处理）；保留防御性分支
    if q == 0.0:
        roots = (-b / (2.0 * a),)
    else:
        roots = (q / a, c / q)
    return tuple(sorted(roots))
