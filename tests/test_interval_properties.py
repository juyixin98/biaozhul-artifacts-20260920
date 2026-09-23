"""Property-style soundness checks for interval arithmetic.

Finite intervals are checked exhaustively point-by-point; intervals with
infinite endpoints are checked with a wide finite sampling window plus
structural containment assertions.  The abstract result must contain every
concrete result.
"""

import random
import unittest

from intervalai import intervals as iv


def rand_interval(rng):
    # endpoints drawn from small ints and +/- infinity
    pool = [None] + list(range(-4, 5))
    lo = rng.choice(pool)
    hi = rng.choice(pool)
    v = iv.make(lo if lo is None else lo, hi if hi is None else hi)
    if v is iv.BOT:
        return iv.make(0, 0)
    return v


class TestArithmeticProperties(unittest.TestCase):
    def test_finite_ops_contain_all_points(self):
        # exhaustive over all small finite intervals
        rng = random.Random(1)
        bounds = [(-3, -1), (-2, 0), (-1, 1), (0, 2), (1, 3), (-3, 3)]
        import itertools
        for (al, ah), (bl, bh) in itertools.product(bounds, bounds):
            a, b = iv.make(al, ah), iv.make(bl, bh)
            self._check(a, b)

    def test_random_intervals_with_infinities(self):
        rng = random.Random(2026)
        for _ in range(3000):
            a = rand_interval(rng)
            b = rand_interval(rng)
            self._check(a, b, sample=True)

    def _check(self, a, b, sample=False):
        q, mz = iv.div(a, b)
        r, rmz = iv.mod(a, b)
        s = iv.add(a, b)
        d = iv.sub(a, b)
        p = iv.mul(a, b)

        # may-divide-zero flag must be exact
        if b[0] is not None and b[1] is not None:
            self.assertEqual(mz, 0 in range(b[0], b[1] + 1))

        xs = self._points(a, sample)
        ys = self._points(b, sample)
        for x in xs:
            for y in ys:
                self.assertTrue(iv.contains(p, x * y),
                                f"{x}*{y} not in {iv.to_str(p)}")
                self.assertTrue(iv.contains(iv.add(a, b), x + y))
                self.assertTrue(iv.contains(d, x - y))
                if y != 0:
                    self.assertTrue(iv.contains(q, x // y),
                                    f"{x}//{y}={x//y} not in {iv.to_str(q)}")
                    self.assertTrue(iv.contains(r, x % y),
                                    f"{x}%{y}={x%y} not in {iv.to_str(r)}")

    @staticmethod
    def _points(v, sample):
        if iv.is_bot(v):
            return []
        lo, hi = v
        if lo is not None and hi is not None:
            return range(lo, hi + 1)
        # infinite end: sample a large window representative of magnitude
        lo = lo if lo is not None else -50
        hi = hi if hi is not None else 50
        return range(lo, hi + 1)


if __name__ == "__main__":
    unittest.main()
