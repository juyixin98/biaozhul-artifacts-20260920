"""Unit tests for the interval abstract domain."""

import unittest

from intervalai import intervals as iv


class TestIntervals(unittest.TestCase):
    def test_lattice(self):
        self.assertTrue(iv.subset(iv.const(2), iv.make(1, 3)))
        self.assertFalse(iv.subset(iv.make(1, 3), iv.const(2)))
        self.assertEqual(iv.join(iv.const(1), iv.const(3)), (1, 3))
        self.assertEqual(iv.meet(iv.make(1, 4), iv.make(3, 8)), (3, 4))
        self.assertEqual(iv.meet(iv.make(1, 2), iv.make(3, 4)), iv.BOT)
        self.assertEqual(iv.meet((0, 5), (None, 4)), (0, 4))
        self.assertEqual(iv.meet((None, 5), (3, None)), (3, 5))

    def test_arithmetic_basic(self):
        self.assertEqual(iv.add((1, 3), (10, 20)), (11, 23))
        self.assertEqual(iv.sub((1, 3), (10, 20)), (-19, -7))
        self.assertEqual(iv.mul((-2, 3), (4, 5)), (-10, 15))
        self.assertEqual(iv.mul((-3, -1), (-2, 4)), (-12, 6))
        self.assertEqual(iv.neg((1, 5)), (-5, -1))

    def test_exhaustive_mul_div_mod_soundness(self):
        # Compare abstract results against all point combinations over a set
        # of small intervals — the abstract interval must contain every
        # concrete result.
        import itertools
        test_ranges = [(-4, -1), (-3, 0), (-2, 2), (0, 3), (1, 4)]
        bounds = [(-4, -1), (-3, -1), (-2, 2), (1, 3), (-3, 3)]
        for (al, ah), (bl, bh) in itertools.product(test_ranges, bounds):
            a = iv.make(al, ah)
            b = iv.make(bl, bh)
            q, mz = iv.div(a, b)
            r, rmz = iv.mod(a, b)
            xs = range(al, ah + 1)
            ys = [y for y in range(bl, bh + 1) if y != 0]
            self.assertTrue(mz == (0 in range(bl, bh + 1)))
            for x, y in itertools.product(xs, ys):
                qc = x // y
                rc = x % y
                self.assertTrue(iv.contains(q, qc),
                                f"{x}//{y}={qc} not in {iv.to_str(q)}")
                self.assertTrue(iv.contains(r, rc),
                                f"{x}%{y}={rc} not in {iv.to_str(r)}")

    def test_widen_narrow(self):
        self.assertEqual(iv.widen((0, 0), (0, 1)), (0, None))
        self.assertEqual(iv.widen((0, 1), (0, 2)), (0, None))
        w = iv.widen((0, 0), (0, 1))
        # narrowing pulls the +oo bound down to the stable computed bound
        self.assertEqual(iv.narrow(w, (0, 5)), (0, 5))
        self.assertEqual(iv.narrow(w, (0, None)), (0, None))

    def test_unbounded_div(self):
        q, _ = iv.div((None, None), (1, 2))
        self.assertEqual(q, (None, None))
        q, _ = iv.div((10, 20), (1, None))
        self.assertEqual(q, (0, 20))
        q, _ = iv.div((10, 20), (None, -1))
        self.assertEqual(q, (-20, 0))


if __name__ == "__main__":
    unittest.main()
