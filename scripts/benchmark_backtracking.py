#!/usr/bin/env python3
"""Benchmark: this Thompson engine vs Python's backtracking ``re`` module.

This script is *demonstration only* — ``re`` is never used by the shipped
engine.  It runs the classic exponential traps on near-matching inputs and
shows that:

* the Thompson engine stays linear and finishes every size;
* Python ``re`` explodes exponentially and is cut off by a per-call timeout.

Run:  PYTHONPATH=. python3 scripts/benchmark_backtracking.py
"""
from __future__ import annotations

import multiprocessing as mp
import time

from regex_automata import compile_pattern


def re_run(pattern: str, text: str, out) -> None:
    import re

    t0 = time.perf_counter()
    re.search(pattern, text)
    out.value = time.perf_counter() - t0


def timed_with_timeout(pattern: str, text: str, timeout: float) -> float | None:
    ctx = mp.get_context("fork")
    out = ctx.Value("d", -1.0)
    proc = ctx.Process(target=re_run, args=(pattern, text, out))
    proc.start()
    proc.join(timeout)
    if proc.is_alive():
        proc.terminate()
        proc.join()
        return None  # timed out = exponential blow-up
    return out.value if out.value >= 0 else None


def engine_time(pattern: str, text: str) -> float:
    compiled = compile_pattern(pattern)
    t0 = time.perf_counter()
    compiled.search(text)
    return time.perf_counter() - t0


def main() -> None:
    # The classic exponential trap: a fixed ambiguous repeat in the *pattern*,
    # with a longer-and-longer near-miss text.  Backtracking is ~2^n here.
    traps = [
        (r"(a+)+b", lambda n: ("a" * n) + "c"),
        (r"(a|aa)*b", lambda n: ("a" * n) + "c"),
        (r"(a|a)*b", lambda n: ("a" * n) + "c"),
    ]
    sizes = [10, 15, 20, 22, 24, 26, 28, 30]
    print(f"{'pattern':<16}{'n':>4}{'thompson(s)':>14}{'python re(s)':>16}")
    for pattern, build in traps:
        for n in sizes:
            text = build(n)
            te = engine_time(pattern, text)
            tr = timed_with_timeout(pattern, text, timeout=2.0)
            re_cell = f"{tr:.6f}" if tr is not None else "> 2.0 (timeout)"
            print(f"{pattern:<16}{n:>4}{te:>14.6f}{re_cell:>16}")
        print()
    print("A 'timeout' cell means Python's backtracking re was killed after 2s;")
    print("the Thompson engine finishes the same case in milliseconds.")


if __name__ == "__main__":
    main()
