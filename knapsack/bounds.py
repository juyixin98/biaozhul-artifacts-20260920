"""可验证的分数背包（线性规划松弛）上界。

对 0/1 背包::

    maximize   sum v_i x_i
    subject to sum w_i x_i <= C,  x_i in {0, 1}

把 ``x_i in {0,1}`` 放松为 ``0 <= x_i <= 1`` 后得到分数背包问题，其贪心
解（按价值/重量密度降序，最后一件可拆分）给出整数最优值的上界 U，且
``floor(U)`` 仍是上界（整数目标值不可能超过 floor(U)）。

为了让上界**可验证、无浮点误差**，这里全程用整数：

* 密度比较采用交叉相乘 ``v1*w2 vs v2*w1``；
* 结果同时返回浮点展示值 ``float_value`` 与精确有理表示
  ``(numerator, denominator)``，满足
  ``float_value ≈ numerator / denominator``；
* 调用方在剪枝时直接使用整数 ``floor``，不触碰浮点。

注意：这里处理的物品全部满足 w_i > 0。零重量物品在预处理阶段单独处理
（见 solver.py），不进入分数上界。
"""

from __future__ import annotations


def fractional_bound(
    values: list[int],
    weights: list[int],
    start: int,
    capacity: int,
) -> tuple[int, int, int, float]:
    """从索引 ``start`` 起、剩余容量 ``capacity`` 的分数背包上界。

    要求 ``values[i], weights[i]`` 已按密度降序排列，且所有
    ``weights[i] > 0``（i >= start）。

    返回 ``(floor_bound, num, den, float_value)``：

    * floor_bound : 上界的整数部分（剪枝只认它）；
    * num, den    : 上界的精确有理表示，bound = num/den；
    * float_value : num/den 的浮点近似，仅用于输出展示。
    """

    # 调用方保证 capacity >= 0；容量耗尽时上界就是当前已得价值 0。
    if capacity <= 0:
        return 0, 0, 1, 0.0

    base = 0          # 整件装入的价值和
    rem = capacity
    n = len(values)
    i = start
    while i < n:
        w = weights[i]
        if w > rem:
            break
        base += values[i]
        rem -= w
        i += 1

    if i >= n:
        # 剩余物品全部整件装入，上界恰为整数 base。
        return base, base, 1, float(base)

    # 临界物品按分数装入 rem / w_i（排序保证它是此刻密度最高的）。
    v = values[i]
    w = weights[i]
    # bound = base + v * rem / w
    num = base * w + v * rem
    den = w
    # 精确整除判断，避免 math.floor 浮点路径。
    floor_bound = num // den if num >= 0 else -((-num + den - 1) // den)
    return floor_bound, num, den, num / den
