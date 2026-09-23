"""终局判定的纯整数数学：严格超过总权重的 2/3。

不使用浮点或分数除法。判定条件 votes * 3 > total * 2，
对任意非负整数权重都精确成立（含 total == 0）。
"""
from __future__ import annotations


def is_strict_supermajority(votes_weight: int, total_weight: int) -> bool:
    """votes_weight / total_weight > 2/3 的精确整数判定。

    total_weight == 0 时永远为 False（没有任何投票权，不可能终局）。
    """
    if total_weight <= 0:
        return False
    if votes_weight < 0:
        return False
    return votes_weight * 3 > total_weight * 2


def required_weight(total_weight: int) -> int:
    """达到终局所需的最小投票权（整数）。

    即 floor(2W/3) + 1；W == 0 时返回 0（此情况下无人能终局）。
    """
    if total_weight <= 0:
        return 0
    return (2 * total_weight) // 3 + 1
