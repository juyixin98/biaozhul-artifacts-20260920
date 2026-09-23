"""Catastrophic-backtracking resistance tests.

Classic pathological patterns cause naive recursive backtracking engines
to take exponential time on strings that *almost* match.  The Thompson
set-based simulation is O(n * |NFA|), so these must run comfortably fast
and, more importantly, must not blow up exponentially as input grows.

We assert both an absolute time ceiling (generous, CI-friendly) and — the
real property — that doubling the input length does not multiply the
runtime by an exponential factor.
"""
from __future__ import annotations

import time
import unittest

from regex_automata import compile_pattern

# (pattern, failing-input-builder) pairs: the canonical exponential traps.
TRAPS = [
    (r"(a?){20}a{20}", lambda n: ("a" * (n - 1)) + "b"),
    (r"(a|a)*b", lambda n: ("a" * n) + "c"),
    (r"(a*)*b", lambda n: ("a" * n) + "c"),
    (r"(a|aa)*b", lambda n: ("a" * n) + "c"),
    (r"(a+)+b", lambda n: ("a" * n) + "c"),
    (r"(a?){n}a{n}".replace("{n}", "{12}"), lambda n: ("a" * n) + "b"),
]


def time_search(pattern: str, text: str, runs: int = 3) -> float:
    compiled = compile_pattern(pattern)
    best = float("inf")
    for _ in range(runs):
        t0 = time.perf_counter()
        compiled.search(text)
        best = min(best, time.perf_counter() - t0)
    return best


class CatastrophicBacktrackingTests(unittest.TestCase):
    def test_traps_finish_fast(self):
        for pattern, build in TRAPS:
            text = build(24)
            elapsed = time_search(pattern, text)
            self.assertLess(
                elapsed, 0.5,
                msg=f"{pattern!r} on {len(text)} chars took {elapsed:.3f}s",
            )

    def test_no_exponential_growth(self):
        # The flagship trap: time at n and 2n should be roughly linear,
        # certainly nowhere near the 2^n blow-up a backtracker shows.
        pattern = r"(a?){20}a{20}"
        sizes = [10, 12, 14, 16, 18, 20, 22]
        timings = [(n, time_search(pattern, ("a" * (n - 1)) + "b", runs=2))
                   for n in sizes]
        # Compare the two largest sizes; a backtracker never finishes n=22.
        n1, t1 = timings[-2]
        n2, t2 = timings[-1]
        self.assertTrue(
            t2 < max(0.25, t1 * 8),
            msg=f"near-blow-up: n={n1}->{t1:.4f}s, n={n2}->{t2:.4f}s "
                f"(timings={[(n, round(t, 4)) for n, t in timings]})",
        )

    def test_nested_star_trap_linear(self):
        pattern = r"(a*)*b"
        timings = []
        for n in (50, 100, 200, 400):
            timings.append((n, time_search(pattern, ("a" * n) + "c", runs=2)))
        (n1, t1), (n2, t2) = timings[0], timings[-1]
        # linear expectation: t(400)/t(50) should be modest; allow generous slack
        self.assertLess(
            t2, max(0.5, t1 * 25),
            msg=f"possibly super-linear: {timings}",
        )

    def test_compile_is_finite_for_nested_quantifiers(self):
        # Must terminate even though the grammar permits nested repeats.
        compile_pattern(r"(a*)*")
        compile_pattern(r"((a|b)*)+")


if __name__ == "__main__":
    print("Timing report (pattern, input length -> best seconds):")
    for pattern, build in TRAPS:
        for n in (12, 20, 24):
            print(f"  {pattern!r:22} n={n:<3} {time_search(pattern, build(n)):.6f}s")
    unittest.main(verbosity=2)
