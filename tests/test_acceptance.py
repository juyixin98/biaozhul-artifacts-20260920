"""Acceptance tests: exhaustive bounded inputs vs. real execution.

Each case runs the concrete interpreter over the full Cartesian product of
its input ranges and checks the interval analysis for (a) soundness — every
real fault is alarmed — and (b) the declared precision level.  Imprecise
cases document the exact false alarms as the conservativity boundary.
"""

import os
import unittest

from tests.diff_engine import run_case

HERE = os.path.dirname(os.path.abspath(__file__))
EX = os.path.join(HERE, "..", "examples")


def load(name):
    with open(os.path.join(EX, name), "r", encoding="utf-8") as f:
        return f.read()


def line_of(src, needle):
    for i, ln in enumerate(src.splitlines(), start=1):
        if needle in ln:
            return i
    raise AssertionError(f"marker {needle!r} not found in source")


CASES = []


def case(name, source, ranges, precision, expected_fp=None, notes=""):
    CASES.append({
        "name": name,
        "source": source,
        "ranges": ranges,
        "precision": precision,
        "expected_false_alarms": expected_fp or [],
        "notes": notes,
    })


# --------------------------------------------------------------- exact cases

def build_cases():
    s = load("p1_safe_loop.imp")
    case("p1_safe_loop_exact", s, {"n": (0, 5)}, "exact",
         notes="loop bounded by n<=5, index provably <5")

    s = load("p2_oob_loop.imp")
    case("p2_oob_loop_exact", s, {"n": (0, 5)}, "exact",
         notes="size 3 array; n=3,4,5 really fault at the store")

    s = load("p4_positive_guard.imp")
    case("p4_positive_guard_exact", s, {"d": (-2, 3)}, "exact",
         notes="d>0 restricts divisor to [1,3]; provably safe")

    s = load("p5_negative_index.imp")
    case("p5_negative_index_exact", s, {"i": (-2, 2)}, "exact",
         notes="negative indices really occur; no index can be >=3")

    s = load("p7_definite_divzero.imp")
    case("p7_definite_divzero_exact", s, {}, "exact",
         notes="divisor exactly 0 on the only path")

    s = load("p9_arithmetic.imp")
    case("p9_arithmetic_exact", s, {"a": (2, 5), "b": (1, 4)}, "exact",
         notes="a+b in [3,9] safe; a*b in [2,20] really OOB for some inputs")

    s = load("p10_load_store_oob.imp")
    case("p10_load_store_oob_exact", s, {"n": (0, 5)}, "exact",
         notes="both the store and the load a[i] fault at n>=3; alarms at "
               "two distinct source lines")

    s = load("p11_nested_loops.imp")
    case("p11_nested_loops_exact", s, {"n": (0, 5)}, "exact",
         notes="nested loops: selective widening keeps a[j] provably in "
               "bounds for n<=5 (j < i <= 4)")


# ------------------------------------------------------- conservativity cases

def build_imprecise_cases():
    s = load("p3_diseq_guard.imp")
    line = line_of(s, "q = 100 / d")
    case("p3_diseq_guard_imprecise", s, {"d": (-2, 2)}, "imprecise",
         expected_fp=[("div_by_zero", line)],
         notes="d!=0 removes a single interior point; intervals cannot "
               "represent the resulting set [-2,-1] U [1,2]")

    s = load("p6_correlation.imp")
    line = line_of(s, "q = 5 / (x - y + 1)")
    case("p6_correlation_imprecise", s, {"x": (-1, 1)}, "imprecise",
         expected_fp=[("div_by_zero", line)],
         notes="y=x on every run so x-y+1 is always 1, but the "
               "non-relational domain computes [-1,1]-[-1,1]+1=[-1,3]")


build_cases()
build_imprecise_cases()


class AcceptanceTests(unittest.TestCase):
    def test_all_cases(self):
        failures = []
        table = []
        for c in CASES:
            r = run_case(c)
            table.append(r)
            if not r["ok"]:
                failures.append((c["name"], r["problems"]))
        # Printed table is the visible evidence of exhaustive checking.
        print("\n%-34s %6s %-9s %7s %7s %7s %7s" %
              ("case", "runs", "precision", "faults", "alarms", "missed",
               "false+"))
        for r in table:
            print("%-34s %6d %-9s %7d %7d %7d %7d  iters=%s/%s" %
                  (r["name"], r["runs"], r["precision"],
                   len(r["observed_faults"]), len(r["alarms"]),
                   len(r["missed"]), len(r["false_alarms"]),
                   r["ascending_iterations"], r["narrow_rounds"]))
        if failures:
            for name, problems in failures:
                print(f"FAIL {name}: {problems}")
            self.fail(f"{len(failures)} acceptance case(s) failed: "
                      + ", ".join(n for n, _ in failures))


class UnboundedCaseTests(unittest.TestCase):
    """Unknown intervals (TOP) must be reported, never treated as safe."""

    def test_unbounded_loop_alarms_oob(self):
        from intervalai import analyze_source
        s = load("p8_unbounded_loop.imp")
        rep = analyze_source(s, input_bounds=None, include_points=False)
        kinds = [a["subkind"] for a in rep["alarms"]]
        self.assertTrue(any("too_large" in k for k in kinds),
                        f"unbounded loop must alarm OOB, got {rep['alarms']}")
        # The induction variable reaches +oo after widening.
        self.assertIsNone(rep["exit_state"]["scalars"]["i"]["hi"])

    def test_unbounded_division_alarms(self):
        from intervalai import analyze_source
        rep = analyze_source("input d;\nvar q = 1 / d;\n",
                             input_bounds=None, include_points=False)
        self.assertEqual(rep["alarms"][0]["kind"], "div_by_zero")

    def test_unbounded_nested_loop_widens_and_alarms(self):
        from intervalai import analyze_source
        s = load("p11_nested_loops.imp")
        rep = analyze_source(s, input_bounds=None, include_points=False)
        scalars = rep["exit_state"]["scalars"]
        # outer induction variable must reach +oo under widening
        self.assertIsNone(scalars["i"]["hi"])
        kinds = [a["subkind"] for a in rep["alarms"]]
        self.assertTrue(any("too_large" in k for k in kinds),
                        f"unbounded nested loop must alarm OOB: {rep['alarms']}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
