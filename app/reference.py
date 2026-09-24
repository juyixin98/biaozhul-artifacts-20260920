"""线性扫描参考实现（纯 Python）。

与合约语义一一对应，用于在集成测试中核对链上二分查找的结果：
  - 同区块更新合并（保留最后一个值）
  - 查询返回不晚于目标块的最后值，找不到返回 0
  - 拒绝未来块
"""
from __future__ import annotations


class ReferenceCheckpoints:
    def __init__(self) -> None:
        # 元素为 [block_number, value]，block_number 严格递增
        self._checkpoints: list[list[int]] = []

    def set_value(self, block_number: int, value: int) -> None:
        if self._checkpoints and self._checkpoints[-1][0] == block_number:
            self._checkpoints[-1][1] = value  # 同块合并
        else:
            self._checkpoints.append([block_number, value])

    def value_at(self, target_block: int, current_block: int) -> int:
        if target_block > current_block:
            raise ValueError(
                f"target block {target_block} is in the future "
                f"(current block: {current_block})"
            )
        # 从尾部线性扫描 —— O(n)，刻意朴素
        for block_number, value in reversed(self._checkpoints):
            if block_number <= target_block:
                return value
        return 0

    @property
    def checkpoints(self) -> list[tuple[int, int]]:
        return [(b, v) for b, v in self._checkpoints]
