"""隔离 / 细化 / 十进制输出 的单元测试。"""
import unittest
from fractions import Fraction as F

from rootisolate import decimalize, isolate, polynomial as P
from rootisolate import sturm as S


def isolate_simple(coeffs, depth=5000):
    f = P.poly(coeffs)
    chain = S.sturm_chain(f)
    B = P.cauchy_bound(f)
    leaves, unresolved = isolate.isolate_factor(chain, f, -B, B, depth)
    return f, chain, leaves, unresolved


class TestIsolation(unittest.TestCase):
    def _assert_count_invariant(self, f, chain, leaves, unresolved):
        B = P.cauchy_bound(f)
        total = S.v_at(chain, -B) - S.v_at(chain, B)
        self.assertEqual(
            len(leaves) + sum(u["interior_count"] for u in unresolved), total)

    def test_two_irrational(self):
        f, chain, leaves, unres = isolate_simple([-2, 0, 1])
        self.assertFalse(unres)
        iv = [L for L in leaves if L["kind"] == "interval"]
        self.assertEqual(len(iv), 2)
        for L in iv:
            self.assertEqual(L["va"] - L["vb"], 1)
            self.assertNotEqual(P.sign_at(f, L["a"]), 0)
            self.assertNotEqual(P.sign_at(f, L["b"]), 0)
        # 两区间互不相交
        self.assertLessEqual(iv[0]["b"], iv[1]["a"])
        self._assert_count_invariant(f, chain, leaves, unres)

    def test_exact_point_root(self):
        f, chain, leaves, unres = isolate_simple([0, -1, 0, 1])  # -1,0,1
        self.assertFalse(unres)
        pts = sorted(L["r"] for L in leaves if L["kind"] == "point")
        self.assertEqual(pts, [F(-1), F(0), F(1)])

    def test_no_real_roots(self):
        f, chain, leaves, unres = isolate_simple([1, 0, 0, 0, 1])
        self.assertEqual(leaves, [])
        self.assertEqual(unres, [])

    def test_close_adjacent_roots(self):
        # (x-1)(10^6 x - (10^6+1)) 的整数展开，两根相距 1e-6
        c = [10 ** 12, -(2 * 10 ** 12 + 10 ** 6), 10 ** 12]
        f, chain, leaves, unres = isolate_simple(c)
        self.assertFalse(unres)
        self.assertEqual(len(leaves), 2)
        # 两孤立区间不相交
        a, b = leaves
        self.assertLessEqual(a["b"], b["a"])
        for L in leaves:
            self.assertEqual(L["va"] - L["vb"], 1)

    def test_refinement_width_and_count(self):
        f, chain, leaves, _ = isolate_simple([-2, 0, 1])
        eps = F(1, 10 ** 18)
        for L in leaves:
            rec, status = isolate.refine_leaf(chain, f, L, eps, 5000)
            self.assertEqual(status, "ok")
            self.assertLessEqual(rec["b"] - rec["a"], eps)
            # 细化后变号数证书不变：仍差 1
            self.assertEqual(rec["va"] - rec["vb"], 1)
            # 根仍在新区间内（端点异号且精确求值非零）
            self.assertNotEqual(P.sign_at(f, rec["a"]), 0)
            self.assertNotEqual(P.sign_at(f, rec["b"]), 0)
            self.assertNotEqual(P.sign_at(f, rec["a"]), P.sign_at(f, rec["b"]))

    def test_refine_hits_exact_root(self):
        # 从一个包围 0 的区间开始细化，应精确命中 0
        f = P.poly([0, 1])           # x
        chain = S.sturm_chain(f)
        leaf = {"kind": "interval", "a": F(-1), "b": F(1), "va": 1,
                "vb": 0, "depth": 0}
        rec, status = isolate.refine_leaf(chain, f, leaf, F(1, 100), 5000)
        self.assertEqual(status, "exact")
        self.assertEqual(rec["kind"], "point")
        self.assertEqual(rec["r"], 0)

    def test_refine_depth_failure_status(self):
        f, chain, leaves, _ = isolate_simple([-2, 0, 1])
        eps = F(1, 10 ** 200)       # 极小，预算故意给很少
        rec, status = isolate.refine_leaf(chain, f, leaves[0], eps, 5)
        self.assertEqual(status, "refine_depth")
        # 即便失败，计数证书与端点仍成立
        self.assertEqual(rec["va"] - rec["vb"], 1)
        self.assertGreater(rec["width"], eps)


class TestDecimalize(unittest.TestCase):
    def test_radius_covers_interval_exactly(self):
        import random
        random.seed(1)
        for _ in range(100):
            # 随机有理区间
            a = F(random.randint(-10000, 10000), random.randint(1, 10000))
            h = F(random.randint(1, 1000), 10 ** random.randint(1, 12))
            b = a + h
            if a == b:
                continue
            info = decimalize.decimalize_interval(a, b)
            mid_dec, rad = info["midpoint"], info["radius_exact"]
            true_mid = (a + b) / 2
            # 严格覆盖：最坏点误差
            self.assertLessEqual(abs(true_mid - mid_dec) + h / 2, rad,
                                 (a, b, info))
            # 半径必须是十进制
            self.assertEqual(rad.denominator == 1 or
                             all(ch == "0" for ch in str(rad.denominator)[1:]),
                             True)

    def test_point_root_decimal(self):
        info = decimalize.decimalize_point(F(1, 3), F(1, 10 ** 12))
        # 0.333333333333 与 1/3 的误差 <= 半径，且半径为 5*10^-13
        self.assertLessEqual(abs(F(1, 3) - info["midpoint"]),
                             info["radius_exact"])

    def test_no_fake_precision_radius_not_smaller_than_halfwidth(self):
        # 宽区间不能给出很小的半径
        info = decimalize.decimalize_interval(F(0), F(1))
        self.assertGreaterEqual(info["radius_exact"], F(1, 2))
        self.assertLess(info["radius_exact"], F(10))


if __name__ == "__main__":
    unittest.main()
