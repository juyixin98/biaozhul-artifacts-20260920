"""Exhaustive differential tests: abstract result vs. concrete execution.

These enumerate every bounded input point of selected programs and assert
the soundness contract:

  * every concrete crash site is reported (no false negatives);
  * every concrete final value lies in an abstract exit interval;
  * the set of *spurious* alarms is inspected to demonstrate the
    conservativity boundary (correlation loss, unknown loop bounds).
"""

import unittest

from interval_ai.diffcheck import exhaustive_check, exhaustive_value_check
from interval_ai.intervals import trunc_div, trunc_rem
from interval_ai.pipeline import analyze_source


class TestCorrelationLoss(unittest.TestCase):
    def test_spurious_alarm_from_correlated_variables(self):
        # y == x always, so (x - y) is identically 0.  The program guards on
        # (x - y != 0); concretely that guard is never true, so the division
        # is unreachable and nothing ever crashes.  But the non-relational
        # interval domain cannot relate x to y, so it cannot discharge the
        # guard/divisor -- the textbook false (spurious) alarm.
        src = """
        input var x;
        var y = x;
        if (x - y != 0) {
            var z = 1 / (x - y);
        }
        """
        rep = exhaustive_check(src, {"x": (-12, 12)})
        self.assertGreater(rep.points_checked, 0)
        self.assertEqual(rep.crashes, {})     # never crashes concretely
        self.assertEqual(rep.missed, [])      # no false negatives
        self.assertTrue(rep.sound)

    def test_correlated_always_crashes_is_reported(self):
        # Control: the same correlation expression WITHOUT the guard does
        # crash on every point and must be reported.
        src = """
        input var x;
        var y = x;
        var z = 1 / (x - y);
        """
        rep = exhaustive_check(src, {"x": (-12, 12)})
        self.assertTrue(rep.crashes)
        self.assertEqual(rep.missed, [])


class TestGuardedIndexExhaustive(unittest.TestCase):
    SRC = """
    input var k;
    input array a[4];
    if (k >= 0) {
        if (k < 4) {
            a[k] = 9;
        }
    }
    """

    def test_no_crash_no_alarm(self):
        rep = exhaustive_check(self.SRC, {"k": (-20, 20)})
        self.assertEqual(rep.crashes, {})
        self.assertEqual(rep.missed, [])
        self.assertEqual(rep.alarm_sites, {})

    def test_exit_values_contained(self):
        vrep = exhaustive_value_check(self.SRC, {"k": (-20, 20)})
        self.assertEqual(vrep.violations, [])


class TestUnknownLoopExhaustive(unittest.TestCase):
    SRC = """
    input var n;
    input array a[4];
    var i = 0;
    while (i < n) {
        a[i] = i;
        i = i + 1;
    }
    """

    def test_real_crashes_when_n_ge_5_and_no_miss(self):
        rep = exhaustive_check(self.SRC, {"n": (-3, 8)})
        crash_inputs = sorted(v[0] for vals in rep.crashes.values()
                              for v in vals)
        # a[4] is accessed at i=0..3; n in 0..4 stays in bounds, n>=5
        # writes i=4 which is out of bounds.
        self.assertEqual(crash_inputs, [5, 6, 7, 8])
        self.assertEqual(rep.missed, [])
        self.assertTrue(rep.sound)

    def test_alarm_is_possible_not_certain(self):
        res = analyze_source(self.SRC)
        idx = [a for a in res.result.alarms
               if a.kind == "index_out_of_bounds"]
        self.assertTrue(idx)
        self.assertTrue(all(a.certainty == "possible" for a in idx))

    def test_exit_values_contained(self):
        vrep = exhaustive_value_check(self.SRC, {"n": (-3, 3)})
        self.assertEqual(vrep.violations, [])


class TestNegativeIndexExhaustive(unittest.TestCase):
    SRC = """
    input array a[4];
    var i = -1;
    while (i > -4) {
        a[i] = 0;
        i = i - 1;
    }
    """

    def test_run_crashes_and_is_reported(self):
        rep = exhaustive_check(self.SRC, {})
        self.assertEqual(rep.points_checked, 1)
        self.assertTrue(rep.crashes)
        self.assertEqual(rep.missed, [])
        # The negative index is definitely out of bounds.
        self.assertTrue(
            any(c == "certain" for c in rep.alarm_sites.values()))


class TestDivisionExhaustive(unittest.TestCase):
    def test_guard_proves_safety_every_point(self):
        src = """
        input var k;
        if (k > 0) { var z = 10 / k; }
        """
        rep = exhaustive_check(src, {"k": (-25, 25)})
        self.assertEqual(rep.crashes, {})
        self.assertEqual(rep.alarm_sites, {})
        self.assertEqual(rep.missed, [])

    def test_unguarded_divisor_real_crash_at_zero(self):
        src = "input var k; var z = 10 / k;"
        rep = exhaustive_check(src, {"k": (-9, 9)})
        crash_inputs = [v[0] for vals in rep.crashes.values() for v in vals]
        self.assertEqual(crash_inputs, [0])
        self.assertEqual(rep.missed, [])

    def test_division_results_invariant(self):
        src = """
        input var k;
        if (k >= 2) {
            if (k <= 6) {
                var z = 100 / k;
            }
        }
        """
        vrep = exhaustive_value_check(src, {"k": (0, 12)})
        self.assertEqual(vrep.violations, [])


class TestModuloExhaustive(unittest.TestCase):
    def test_remainder_guard_safe_and_unguarded_crashes(self):
        src = """
        input var a;
        input var m;
        if (a >= 0) {
            if (m >= 2) {
                if (m <= 5) {
                    var r = a % m;
                }
            }
        }
        var s = 7 % a;
        """
        rep = exhaustive_check(src, {"a": (-8, 20), "m": (-3, 8)})
        # Unguarded 7 % a crashes exactly when a == 0 (all m values).
        crash_a = sorted({v[0] for vals in rep.crashes.values()
                          for v in vals if v[0] == 0})
        self.assertEqual(crash_a, [0])
        # No crash for a != 0 (the guarded modulo is safe by construction).
        bad = sorted({v[0] for vals in rep.crashes.values()
                      for v in vals if v[0] != 0})
        self.assertEqual(bad, [])
        self.assertEqual(rep.missed, [])


class TestTruncatedSemantics(unittest.TestCase):
    def test_trunc_div_rem_table(self):
        # Exhaustively check the trunc-toward-zero definition and the
        # dividend-sign remainder against explicit expected values.
        expected_div = {
            (7, 2): 3, (-7, 2): -3, (7, -2): -3, (-7, -2): 3,
            (1, 3): 0, (-1, 3): 0, (1, -3): 0, (-1, -3): 0,
            (0, 5): 0, (0, -5): 0, (6, -4): -1, (-6, -4): 1,
        }
        for (a, b), q in expected_div.items():
            self.assertEqual(trunc_div(a, b), q, f"div {a},{b}")
        expected_rem = {
            (7, 2): 1, (-7, 2): -1, (7, -2): 1, (-7, -2): -1,
            (6, -4): 2, (-6, -4): -2,
        }
        for (a, b), r in expected_rem.items():
            self.assertEqual(trunc_rem(a, b), r, f"rem {a},{b}")
            self.assertEqual(trunc_rem(a, b), a - trunc_div(a, b) * b)

    def test_division_by_zero_is_crash_concretely(self):
        src = """
        input var a;
        input var b;
        if (b != 0) {
            var q = a / b;
            var r = a % b;
        }
        """
        rep = exhaustive_check(src, {"a": (-6, 6), "b": (-4, 4)})
        # b==0 excluded by guard: no crash and no miss.
        self.assertEqual(rep.crashes, {})
        self.assertEqual(rep.missed, [])


class TestSpuriousAlarmPrograms(unittest.TestCase):
    def test_spurious_correlation_is_warning_without_crash(self):
        src = """
        input var x;
        var y = x;
        if (x - y != 0) {
            var z = 1 / (x - y);
        }
        """
        rep = exhaustive_check(src, {"x": (-30, 30)})
        self.assertEqual(rep.crashes, {})
        # The analyzer warns (it cannot relate x to y)...
        self.assertTrue(
            any(k.startswith("div_by_zero") for k in rep.alarm_sites))
        # ...and that single alarm is the spurious conservativity boundary.
        self.assertEqual(len(rep.spurious), 1)
        self.assertEqual(rep.missed, [])

    def test_spurious_array_contents_top(self):
        src = """
        input array a[4];
        a[0] = 5;
        var c = a[0];
        var q = 100 / c;
        """
        rep = exhaustive_check(src, {})
        self.assertEqual(rep.crashes, {})
        self.assertTrue(
            any(k.startswith("div_by_zero") for k in rep.alarm_sites))
        self.assertEqual(rep.missed, [])
        self.assertTrue(rep.spurious)


class TestBroadEnumeration(unittest.TestCase):
    def test_thousands_of_points_stay_sound(self):
        # 41 * 21 = 861 points; the guards exclude the zero divisor.
        src = """
        input var x;
        input var y;
        if (x >= 0) {
            if (y >= 1) {
                if (y <= 10) {
                    var z = (x + 1) / y;
                }
            }
        }
        """
        rep = exhaustive_check(src, {"x": (0, 40), "y": (-5, 15)})
        self.assertEqual(rep.points_checked, 41 * 21)
        self.assertEqual(rep.crashes, {})
        self.assertEqual(rep.missed, [])
        vrep = exhaustive_value_check(src, {"x": (0, 40), "y": (-5, 15)})
        self.assertEqual(vrep.violations, [])


if __name__ == "__main__":
    unittest.main()
