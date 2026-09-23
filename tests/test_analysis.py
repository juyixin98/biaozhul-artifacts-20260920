"""Tests for interval analysis behavior on fixed programs."""

import unittest

from interval_ai.analyzer import Analyzer
from interval_ai.pipeline import analyze_source
from interval_ai.parser import parse_source
from interval_ai.ir import build_cfg


def analyze(src):
    return analyze_source(src)


def alarm_kinds(res):
    return [(a.kind, a.certainty, a.span.start_offset)
            for a in res.result.alarms]


def block_invariant(res, b):
    return res.result.invariants[b]


class TestGuardsAndPrecision(unittest.TestCase):
    def test_guard_proves_divisor_nonzero(self):
        src = """
        input var k;
        if (k > 0) { var z = 10 / k; }
        """
        res = analyze(src)
        self.assertEqual(res.result.alarms, [])

    def test_guard_leq_nonzero(self):
        src = """
        input var k;
        if (k >= 1) { var z = 10 / k; }
        """
        self.assertEqual(analyze(src).result.alarms, [])

    def test_unknown_input_divisor_alarmed(self):
        src = "input var k; var z = 10 / k;"
        res = analyze(src)
        kinds = [a.kind for a in res.result.alarms]
        self.assertIn("div_by_zero", kinds)
        # k genuinely ranges over all ints -> possible, not certain.
        a = res.result.alarms[0]
        self.assertEqual(a.certainty, "possible")

    def test_certain_division_by_zero(self):
        src = "var z = 1 / 0;"
        res = analyze(src)
        self.assertEqual(len(res.result.alarms), 1)
        self.assertEqual(res.result.alarms[0].kind, "div_by_zero")
        self.assertEqual(res.result.alarms[0].certainty, "certain")

    def test_modulo_by_zero_alarmed(self):
        src = "input var k; var z = 7 % k;"
        kinds = [a.kind for a in analyze(src).result.alarms]
        self.assertIn("div_by_zero", kinds)

    def test_unreachable_code_no_alarms(self):
        # The then-block is unreachable (condition always false); the
        # division there must not be reported.
        src = """
        if (false) { var z = 1 / 0; }
        skip;
        """
        res = analyze(src)
        self.assertEqual(res.result.alarms, [])

    def test_contradictory_guard_makes_unreachable(self):
        src = """
        var x = 5;
        if (x < 0) { var z = 1 / 0; }
        """
        self.assertEqual(analyze(src).result.alarms, [])


class TestLoopsWideningNarrowing(unittest.TestCase):
    def test_bounded_loop_invariant(self):
        src = """
        var i = 0;
        while (i < 10) { i = i + 1; }
        """
        res = analyze(src)
        # Find header block: it has a branch.
        cfg = res.result.cfg
        header = next(b.id for b in cfg.blocks
                      if hasattr(b.terminator, "cond"))
        inv = res.result.invariants[header]["i"]
        self.assertEqual(inv.lo, 0)
        self.assertEqual(inv.hi, 10)     # narrowing recovered finite bound

    def test_loop_exit_is_point(self):
        src = """
        var i = 0;
        while (i < 10) { i = i + 1; }
        """
        res = analyze(src)
        # The block reached on the false edge has i == 10.
        # It is the block containing no loop-body successors.
        post = None
        for b in res.result.cfg.blocks:
            t = b.terminator
            if hasattr(t, "cond"):
                else_b = res.result.invariants[t.else_]["i"]
                self.assertEqual(else_b.lo, 10)
                self.assertEqual(else_b.hi, 10)
                post = t.else_
        self.assertIsNotNone(post)

    def test_decrementing_loop_narrows_lower_bound(self):
        src = """
        var i = 10;
        while (i > 0) { i = i - 1; }
        """
        res = analyze(src)
        for b in res.result.cfg.blocks:
            t = b.terminator
            if hasattr(t, "cond"):
                inv = res.result.invariants[b.id]["i"]
                self.assertEqual(inv.lo, 0)
                self.assertEqual(inv.hi, 10)

    def test_unknown_bound_loop_is_top_high(self):
        # Loop bounded by unknown input: high bound must remain +inf.
        src = """
        input var n;
        var i = 0;
        while (i < n) { i = i + 1; }
        """
        res = analyze(src)
        for b in res.result.cfg.blocks:
            t = b.terminator
            if hasattr(t, "cond"):
                inv = res.result.invariants[b.id]["i"]
                self.assertEqual(inv.lo, 0)
                self.assertIsNone(inv.hi)

    def test_loop_with_division_counter_safe(self):
        # j = 100 / (10 - i) where i in [0,9] in the body (guard i<10).
        src = """
        var i = 0;
        while (i < 10) {
            var j = 100 / (10 - i);
            i = i + 1;
        }
        """
        res = analyze(src)
        # Inside the body i <= 9 so (10-i) >= 1: no zero.  Classical
        # narrowing of i plus the guard is enough to prove safety.
        self.assertEqual(
            [a.kind for a in res.result.alarms], [])


    def test_nested_loops_both_bounded_precise(self):
        src = """
        var i = 0;
        while (i < 4) {
            var j = 0;
            while (j < 3) { j = j + 1; }
            i = i + 1;
        }
        """
        res = analyze(src)
        header_ivs = []
        for b in res.result.cfg.blocks:
            if hasattr(b.terminator, "cond"):
                header_ivs.append(
                    (res.result.invariants[b.id]["i"],
                     res.result.invariants[b.id]["j"]))
        # Outer header i in [0,4], inner header j in [0,3].
        i_hi = {iv[0].hi for iv in header_ivs}
        j_hi = {iv[1].hi for iv in header_ivs}
        self.assertIn(4, i_hi)
        self.assertIn(3, j_hi)

    def test_nested_loops_unknown_outer_stays_top(self):
        src = """
        input var outer;
        var i = 0;
        while (i < outer) {
            var j = 0;
            while (j < outer) { j = j + 1; }
            i = i + 1;
        }
        """
        res = analyze(src)
        outer_headers = []
        for b in res.result.cfg.blocks:
            if hasattr(b.terminator, "cond"):
                iv = res.result.invariants[b.id]["i"]
                outer_headers.append(iv)
        # The outer counter must retain hi=+inf (must not be spuriously
        # tightened to a finite value by inner-loop narrowing ordering).
        self.assertTrue(any(iv.hi is None for iv in outer_headers))


class TestArrays(unittest.TestCase):
    def test_in_bounds_constant_index(self):
        src = "input array a[4]; a[0] = 1; var x = a[3];"
        self.assertEqual(analyze(src).result.alarms, [])

    def test_negative_index_certain_oob(self):
        src = "input array a[4]; a[-1] = 1;"
        res = analyze(src)
        kinds = alarm_kinds(res)
        self.assertTrue(any(k == "index_out_of_bounds" and c == "certain"
                            for k, c, _ in kinds))

    def test_index_equal_length_certain_oob(self):
        src = "input array a[4]; var x = a[4];"
        res = analyze(src)
        a = next(x for x in res.result.alarms
                 if x.kind == "index_out_of_bounds")
        self.assertEqual(a.certainty, "certain")

    def test_unknown_index_possible_oob(self):
        src = "input var i; input array a[4]; var x = a[i];"
        res = analyze(src)
        a = next(x for x in res.result.alarms
                 if x.kind == "index_out_of_bounds")
        self.assertEqual(a.certainty, "possible")

    def test_guarded_negative_index_proved_safe(self):
        # i constrained to [0,3] by two one-sided guards.
        src = """
        input var i;
        input array a[4];
        if (i >= 0) {
            if (i < 4) {
                a[i] = 9;
            }
        }
        """
        self.assertEqual(analyze(src).result.alarms, [])

    def test_negative_loop_index(self):
        # A negative-going counter indexing an array: i in [-3,-1] is
        # definitely out of bounds for a[4].
        src = """
        input array a[4];
        var i = -1;
        while (i > -4) {
            a[i] = 1;
            i = i - 1;
        }
        """
        res = analyze(src)
        kinds = [a.kind for a in res.result.alarms]
        self.assertIn("index_out_of_bounds", kinds)

    def test_array_load_is_top(self):
        src = "input array a[4]; var x = a[0]; var z = 1 / x;"
        res = analyze(src)
        # a[0] is an unknown integer -> divisor alarm, unknown must NOT be
        # treated as safe.
        kinds = [a.kind for a in res.result.alarms]
        self.assertIn("div_by_zero", kinds)


class TestAlarmsCarryLocations(unittest.TestCase):
    def test_alarm_points_at_operator_and_source_line(self):
        src = "var a = 1;\nvar b = 0;\nvar c = a / b;"
        res = analyze(src)
        a = res.result.alarms[0]
        self.assertEqual(a.span.line, 3)
        self.assertEqual(src[a.span.start_offset:a.span.end_offset], "/")

    def test_index_alarm_points_at_bracket_name(self):
        src = "input array a[4];\na[7] = 1;"
        res = analyze(src)
        a = res.result.alarms[0]
        self.assertEqual(a.span.line, 2)
        self.assertEqual(src[a.span.start_offset], "a")


class TestLargeIntegers(unittest.TestCase):
    def test_arithmetic_is_mathematical(self):
        big = "123456789012345678901234567890"
        # Force a successor block: invariants are block *entry* states, and
        # the assignment executes inside the entry block.
        src = f"var x = {big} + {big};\nif (true) {{ skip; }}"
        res = analyze(src)
        found = [st["x"] for st in res.result.invariants
                 if st is not None and "x" in st
                 and st["x"].lo is not None]
        self.assertTrue(found, "expected a block with finite x")
        self.assertTrue(all(st.lo == 2 * int(big) for st in found))
        self.assertTrue(all(st.hi == 2 * int(big) for st in found))


if __name__ == "__main__":
    unittest.main()
