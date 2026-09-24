"""确定性伪随机数: SplitMix64。

不依赖 random 模块的全局状态, 相同 (seed, 调用次数) 在任何机器、任何
Python 版本上都产生相同序列, 保证实验可复现。
"""

MASK64 = (1 << 64) - 1


class SplitMix64:
    def __init__(self, seed: int) -> None:
        self.state = seed & MASK64

    def next_u64(self) -> int:
        self.state = (self.state + 0x9E3779B97F4A7C15) & MASK64
        z = self.state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK64
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK64
        return z ^ (z >> 31)

    def uniform(self) -> float:
        """返回 [0, 1) 区间内的确定性浮点数。"""
        return self.next_u64() / (1 << 64)
