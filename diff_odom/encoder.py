"""编码器计数器回绕处理。"""

from __future__ import annotations


def unwrap_count_delta(
    previous: int, current: int, count_min: int, count_max: int
) -> tuple[int, bool]:
    """计算两次读数之间的有符号增量，自动处理计数器回绕。

    计数器在 [count_min, count_max] 内循环。假设真实位移对应的
    计数变化在单个采样周期内不超过半个量程（采样定理约束），
    否则回绕方向无法唯一确定——该约束在 README 中说明。

    Args:
        previous: 上一次原始读数。
        current: 本次原始读数。
        count_min: 计数器下限（含）。
        count_max: 计数器上限（含）。

    Returns:
        (delta, wrapped): 有符号增量与是否发生了回绕修正。

    Raises:
        ValueError: 读数超出 [count_min, count_max]。
    """
    for name, value in (("previous", previous), ("current", current)):
        if not count_min <= value <= count_max:
            raise ValueError(
                f"读数 {name}={value} 超出计数器范围 [{count_min}, {count_max}]"
            )

    count_range = count_max - count_min + 1
    delta = current - previous
    wrapped = False
    if delta > count_range / 2:
        delta -= count_range
        wrapped = True
    elif delta < -count_range / 2:
        delta += count_range
        wrapped = True
    return delta, wrapped
