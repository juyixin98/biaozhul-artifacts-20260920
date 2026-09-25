"""灾难性回溯（catastrophic backtracking）验证。

两个互补的断言：
  1. 朴素回溯参考解释器在经典模式上搜索空间指数增长（步数预算被迅速耗尽），
     证明该模式确实是指数型样本；
  2. Thompson NFA 引擎在同样输入上线性——比较 n 与 2n 时的模拟"边检查"
     工作量，比值应接近 2（远低于任何指数曲线），且墙钟时间保持毫秒级。
"""

import time
import unittest

from renfa import Regex, reference
from renfa.reference import BudgetExhausted


class CatastrophicBacktrackingTests(unittest.TestCase):
    PATTERN_NESTED = "(a+)+b"
    PATTERN_OPTIONAL = "(a?){25}a{25}"
    def test_reference_blows_up_nested(self):
        # 全 a 无 b：(a+)+ 有 2^(n-1) 种切分方式
        # 实测步数约为 2^(n+1)：n=12 约 3.3 万，n=16 约 52 万
        for n, budget in ((12, 25_000), (16, 400_000)):
            with self.subTest(n=n):
                with self.assertRaises(BudgetExhausted):
                    reference.fullmatch(self.PATTERN_NESTED, "a" * n,
                                        budget=budget)

    def test_reference_steps_grow_exponentially(self):
        # 步数应随 n 至少指数增长（每 +1 约翻倍），小 n 下可算完
        s14 = reference.steps_used_fullmatch(self.PATTERN_NESTED, "a" * 14,
                                             budget=10_000_000)
        s15 = reference.steps_used_fullmatch(self.PATTERN_NESTED, "a" * 15,
                                             budget=10_000_000)
        ratio = s15 / s14
        self.assertGreater(ratio, 1.7)  # 接近翻倍，明确的指数特征

    def test_reference_optional_pattern_blows_up(self):
        with self.assertRaises(BudgetExhausted):
            reference.fullmatch(self.PATTERN_OPTIONAL, "a" * 24,
                                budget=2_000_000)

    def _engine_work(self, pattern: str, n: int) -> tuple[int, float, bool]:
        regex = Regex(pattern)
        text = "a" * n
        t0 = time.perf_counter()
        result = regex.fullmatch(text)
        elapsed = time.perf_counter() - t0
        return regex.last_steps, elapsed, result

    def test_thompson_is_linear_nested(self):
        r16, t16, ok16 = self._engine_work(self.PATTERN_NESTED, 16)
        r32, t32, ok32 = self._engine_work(self.PATTERN_NESTED, 32)
        r64, t64, ok64 = self._engine_work(self.PATTERN_NESTED, 64)
        self.assertFalse(ok16)
        self.assertFalse(ok32)
        self.assertFalse(ok64)
        # 工作量近似线性翻倍（NFA 固定，每位置活跃状态集大小有界）
        self.assertLess(r32 / r16, 2.6)
        self.assertLess(r64 / r32, 2.6)
        # 墙钟时间保持毫秒级（参考解释器在同规模 n=20 已无法完成）
        self.assertLess(t64, 0.1)

    def test_thompson_is_linear_optional(self):
        # (a?){25}a{25}：NFA 状态数固定约 100+
        r24, t24, ok24 = self._engine_work(self.PATTERN_OPTIONAL, 24)
        r48, t48, ok48 = self._engine_work(self.PATTERN_OPTIONAL, 48)
        self.assertFalse(ok24)
        self.assertTrue(ok48)
        self.assertLess(r48 / max(r24, 1), 2.8)
        self.assertLess(t48, 0.2)

    def test_thompson_large_input_stays_fast(self):
        # 10 万个 a：线性引擎应在很短时间内完成
        regex = Regex(self.PATTERN_NESTED)
        t0 = time.perf_counter()
        result = regex.fullmatch("a" * 100_000)
        elapsed = time.perf_counter() - t0
        self.assertFalse(result)
        self.assertLess(elapsed, 2.0)


if __name__ == "__main__":
    unittest.main()
