"""Unit tests for the interval abstract domain."""

import unittest

from interval_ai.intervals import Interval as I, trunc_div, trunc_rem


class TestTruncSemantics(unittest.TestCase):
    def test_trunc_div_table(self):
        # Truncation toward zero, unlike Python floor division.
        cases = {
            (7, 2): 3, (-7, 2): -3, (7, -2): -3, (-7, -2): 3,
            (0, 5): 0, (-1, 2): 0, (-9, 4): -2,
        }
        for (a, b), q in cases.items():
            self.assertEqual(trunc_div(a, b), q)

    def test_trunc_rem_sign_follows_dividend(self):
        self.assertEqual(trunc_rem(-7, 2), -1)
        self.assertEqual(trunc_rem(7, -2), 1)
        self.assertEqual(trunc_rem(-7, -2), -1)
        self.assertEqual(trunc_rem(7, 2), 1)


class TestLattice(unittest.TestCase):
    def test_bottom_join(self):
        b = I.bottom()
        self.assertTrue(b.is_bottom)
        self.assertEqual(b.join(I.point(3)), I.point(3))
        self.assertTrue(b.subset_eq(I.top()))
        self.assertTrue(b.subset_eq(b))

    def test_join_hull(self):
        self.assertEqual(I.ranged(1, 4).join(I.ranged(10, 20)),
                         I.ranged(1, 20))
        self.assertEqual(I.ranged(None, 4).join(I.ranged(10, None)), I.top())

    def test_meet(self):
        self.assertEqual(I.ranged(1, 10).meet(I.ranged(5, 20)),
                         I.ranged(5, 10))
        self.assertTrue(I.ranged(1, 2).meet(I.ranged(5, 6)).is_bottom)

    def test_contains(self):
        self.assertTrue(I.ranged(-3, 3).contains(0))
        self.assertFalse(I.ranged(-3, 3).contains(4))
        self.assertFalse(I.bottom().contains(0))
        self.assertTrue(I.top().contains(10**40))


class TestArithmetic(unittest.TestCase):
    def test_add_sub_finite(self):
        self.assertEqual(I.ranged(1, 3).add(I.ranged(10, 20)),
                         I.ranged(11, 23))
        self.assertEqual(I.ranged(1, 3).sub(I.ranged(10, 20)),
                         I.ranged(-19, -7))

    def test_add_with_infinity(self):
        self.assertEqual(I.ranged(1, None).add(I.ranged(-10, 2)),
                         I.ranged(-9, None))
        # [-inf,0] - [0,+inf] = [-inf, 0] (hi = 0-0; lo = -inf-(+inf)).
        self.assertEqual(I.ranged(None, 0).sub(I.ranged(0, None)),
                         I.ranged(None, 0))

    def test_neg(self):
        self.assertEqual(I.ranged(2, 5).neg(), I.ranged(-5, -2))
        self.assertEqual(I.ranged(None, -1).neg(), I.ranged(1, None))

    def test_mult_finite(self):
        self.assertEqual(I.ranged(1, 3).mult(I.ranged(-2, 4)),
                         I.ranged(-6, 12))
        self.assertEqual(I.ranged(-3, -1).mult(I.ranged(2, 5)),
                         I.ranged(-15, -2))

    def test_mult_infinite_signs(self):
        # [-inf, -5] * [2,3] = [-inf, -10]
        self.assertEqual(I.ranged(None, -5).mult(I.ranged(2, 3)),
                         I.ranged(None, -10))
        # [-inf, 2] * [3,4] = [-inf, 8]
        self.assertEqual(I.ranged(None, 2).mult(I.ranged(3, 4)),
                         I.ranged(None, 8))
        # [-1,2] * [-inf,3] = [-inf, +inf]
        self.assertEqual(I.ranged(-1, 2).mult(I.ranged(None, 3)), I.top())
        # [0,0] * top = [0,0] (inf*0 collapses to 0)
        self.assertEqual(I.point(0).mult(I.top()), I.point(0))
        # [1,1] * top = top
        self.assertEqual(I.point(1).mult(I.top()), I.top())

    def test_div_positive(self):
        self.assertEqual(I.ranged(0, 10).div(I.ranged(2, 3)),
                         I.ranged(0, 5))

    def test_div_negative_divisor(self):
        self.assertEqual(I.ranged(0, 10).div(I.ranged(-3, -2)),
                         I.ranged(-5, 0))

    def test_div_split_around_zero(self):
        # Divisor contains 0 (the caller alarms separately): the result over
        # non-crashing executions is the join over positive [1,2] and
        # negative [-2,-1] divisors, computed exactly here as [-10,10].
        self.assertEqual(I.ranged(1, 10).div(I.ranged(-2, 2)),
                         I.ranged(-10, 10))
        # A dividend spanning both signs over a divisor straddling 0 is
        # still exactly bounded here (dividend is finite).
        self.assertEqual(I.ranged(-10, 10).div(I.ranged(-1, 1)),
                         I.ranged(-10, 10))
        # An *unbounded* dividend gives unbounded quotient.
        self.assertEqual(I.ranged(None, 10).div(I.ranged(-1, 1)), I.top())
        # But a dividend known 0 yields 0 whenever it does not crash.
        self.assertEqual(I.point(0).div(I.ranged(-2, 2)), I.point(0))

    def test_div_unbounded_divisor_tends_to_zero(self):
        self.assertEqual(I.point(100).div(I.ranged(3, None)),
                         I.ranged(0, 33))
        self.assertEqual(I.ranged(3, None).div(I.ranged(3, None)),
                         I.ranged(1, None))
        self.assertEqual(I.point(10).div(I.ranged(None, -2)),
                         I.ranged(-5, 0))

    def test_div_is_sound_by_enumeration(self):
        # Differential check of the abstract quotient against trunc_div on a
        # grid, including intervals that straddle sign and infinity edges.
        from interval_ai.intervals import trunc_div
        cases = [
            (I.ranged(-10, 10), I.ranged(2, 5)),
            (I.ranged(-10, 10), I.ranged(-5, -2)),
            (I.ranged(0, 20), I.ranged(1, 6)),
            (I.ranged(-20, -1), I.ranged(1, 6)),
            (I.ranged(-7, 7), I.ranged(-3, 3)),
        ]
        for a, b in cases:
            got = a.div(b)
            xs = [x for x in range(a.lo, a.hi + 1)]
            ys = [y for y in range(b.lo, b.hi + 1) if y != 0]
            for x in xs:
                for y in ys:
                    self.assertTrue(
                        got.contains(trunc_div(x, y)),
                        msg=f"{x}/{y}={trunc_div(x,y)} not in {got}")

    def test_rem_bounds(self):
        self.assertEqual(I.ranged(0, 100).rem(I.ranged(1, 7)),
                         I.ranged(0, 6))
        self.assertEqual(I.ranged(-100, -1).rem(I.ranged(1, 7)),
                         I.ranged(-6, 0))
        self.assertEqual(I.ranged(-50, 50).rem(I.ranged(1, 7)),
                         I.ranged(-6, 6))


class TestWidenNarrow(unittest.TestCase):
    def test_widen_pushes_unstable_bounds(self):
        old = I.ranged(0, 0)
        new = I.ranged(0, 1)
        self.assertEqual(old.widen(new), I.ranged(0, None))

    def test_widen_stable_bound_kept(self):
        old = I.ranged(0, None)
        new = I.ranged(0, 9)
        self.assertEqual(old.widen(new), I.ranged(0, None))

    def test_narrow_refines_only_infinite(self):
        # Classical narrowing after widening: x: [0,+inf] narrowed by the
        # guard-derived iterate [0,10] recovers hi=10, lo stays.
        self.assertEqual(I.ranged(0, None).narrow(I.ranged(0, 10)),
                         I.ranged(0, 10))
        # Finite bounds must never be loosened.
        self.assertEqual(I.ranged(0, 5).narrow(I.ranged(0, 3)),
                         I.ranged(0, 5))

    def test_widen_narrow_loop_simulation(self):
        # Simulate x=0; while(x<10){x++} on the header.
        x = I.bottom()
        # first iterate from outside
        x = x.join(I.point(0))
        seq = []
        # back edges with incrementing x; widening phase
        prev = x
        for k in range(1, 12):
            cand = prev.join(I.point(k))
            w = prev.widen(cand)
            seq.append(w)
            prev = w
            if w == I.ranged(0, None):
                break
        self.assertEqual(prev, I.ranged(0, None))
        # narrowing with body semantics x<10 -> hi 9 at body; header <=10
        narrowed = I.ranged(0, None).narrow(I.ranged(0, 10))
        self.assertEqual(narrowed, I.ranged(0, 10))


class TestFilters(unittest.TestCase):
    def test_comparison_filters(self):
        t = I.top()
        self.assertEqual(t.filter_less(5), I.ranged(None, 4))
        self.assertEqual(t.filter_geq(2), I.ranged(2, None))
        self.assertTrue(I.ranged(0, 3).filter_greater(10).is_bottom)


class TestArithmeticSoundnessByEnumeration(unittest.TestCase):
    """Exhaustively verify abstract arithmetic over a finite grid.

    For every pair of small integer intervals we compare the abstract result
    against the exact set of pointwise results; every concrete value must be
    contained (soundness), and for +,-,* the result must be the tightest
    possible interval (completeness on finite boxes).
    """

    def test_add_sub_mul_tight_on_grid(self):
        from itertools import product
        small = list(range(-3, 4))
        ivs = [I.ranged(lo, hi) for lo in small for hi in small
               if lo <= hi]

        def exact_bin(xs, ys, f):
            vals = [f(a, b) for a in xs for b in ys]
            return I.ranged(min(vals), max(vals))

        for a, b in product(ivs, ivs):
            xs = list(range(a.lo, a.hi + 1))
            ys = list(range(b.lo, b.hi + 1))
            self.assertEqual(a.add(b), exact_bin(xs, ys, lambda x, y: x + y),
                             msg=f"add {a}+{b}")
            self.assertEqual(a.sub(b), exact_bin(xs, ys, lambda x, y: x - y),
                             msg=f"sub {a}-{b}")
            self.assertEqual(a.mult(b), exact_bin(xs, ys, lambda x, y: x * y),
                             msg=f"mul {a}*{b}")

    def test_div_rem_sound_on_grid(self):
        # Division/remainder skip divisor intervals containing 0 for the
        # pointwise comparison of "non-crashing" results; we split exactly
        # like the implementation and check containment of every value.
        for lo in range(-4, 5):
            for hi in range(lo, 5):
                a = I.ranged(lo, hi)
                for lo2 in range(-4, 5):
                    for hi2 in range(lo2, 5):
                        if lo2 == 0 and hi2 == 0:
                            continue
                        b = I.ranged(lo2, hi2)
                        got_div = a.div(b)
                        got_rem = a.rem(b)
                        for x in range(lo, hi + 1):
                            for y in range(lo2, hi2 + 1):
                                if y == 0:
                                    continue
                                self.assertTrue(
                                    got_div.contains(trunc_div(x, y)),
                                    msg=f"{x}/{y} not in {got_div}")
                                self.assertTrue(
                                    got_rem.contains(trunc_rem(x, y)),
                                    msg=f"{x}%{y} not in {got_rem}")


if __name__ == "__main__":
    unittest.main()
