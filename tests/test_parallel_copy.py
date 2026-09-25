"""并行复制串行化：固定用例 + 大规模随机等价性检验。"""

import random
import unittest

from ssa_tool.ir import Const, Name
from ssa_tool.parallel_copy import sequentialize


def simulate(pairs, init):
    env = dict(init)
    for m in sequentialize(pairs):
        env[m.dest] = env[m.src.name] if isinstance(m.src, Name) else m.src.value
    return env


class TestParallelCopy(unittest.TestCase):
    def assert_equivalent(self, pairs, init):
        env = simulate(pairs, init)
        expected = {d: init[s.name] if isinstance(s, Name) else s.value
                    for d, s in pairs}
        for d, v in expected.items():
            self.assertEqual(env[d], v,
                             msg=f"pairs={pairs} init={init} got={env}")

    def test_plain(self):
        self.assert_equivalent(
            [("a", Name("x")), ("b", Name("y"))],
            {"x": 1, "y": 2, "a": 0, "b": 0})

    def test_swap2(self):
        self.assert_equivalent(
            [("a", Name("b")), ("b", Name("a"))], {"a": 1, "b": 2})

    def test_cycle3(self):
        self.assert_equivalent(
            [("a", Name("b")), ("b", Name("c")), ("c", Name("a"))],
            {"a": 1, "b": 2, "c": 3})

    def test_cycle_with_outsider(self):
        # c <- a 必须读到旧 a（=1），而不是换完后的值
        self.assert_equivalent(
            [("a", Name("b")), ("b", Name("a")), ("c", Name("a"))],
            {"a": 1, "b": 2, "c": 9})

    def test_self_and_const(self):
        self.assert_equivalent(
            [("a", Name("a")), ("b", Const(0)), ("c", Name("b"))],
            {"a": 7, "b": 4, "c": 9})

    def test_two_cycles(self):
        self.assert_equivalent(
            [("a", Name("b")), ("b", Name("a")),
             ("c", Name("d")), ("d", Name("c"))],
            {"a": 1, "b": 2, "c": 3, "d": 4})

    def test_temp_required(self):
        # 交换环必须出现至少一个临时量
        moves = sequentialize([("a", Name("b")), ("b", Name("a"))])
        self.assertTrue(any(m.dest.startswith("pcopy.") for m in moves))

    def test_random_equivalence(self):
        random.seed(20260923)
        for _ in range(50_000):
            n = random.randint(1, 9)
            names = [f"v{i}" for i in range(n)]
            # 允许部分源为立即数
            init = {x: random.randint(0, 99) for x in names}
            pairs = []
            for d in names:
                if random.random() < 0.1:
                    pairs.append((d, Const(random.randint(0, 99))))
                else:
                    pairs.append((d, Name(random.choice(names))))
            self.assert_equivalent(pairs, init)


if __name__ == "__main__":
    unittest.main()
