"""Randomized differential fuzzing: generated programs vs. real execution.

A fixed-seed generator produces terminating Imp programs containing

* one or two bounded integer inputs,
* straight-line arithmetic (including / and %),
* array loads/stores with computed (sometimes negative or too-large) indices,
* constant-trip-count loops with computed body indices,
* divisions with and without guards.

For every generated program we exhaustively run the concrete interpreter on
the Cartesian product of the input ranges, then assert SOUNDNESS: each fault
really observed on some run must be covered by an interval-analysis alarm at
the same source line.  False positives are allowed (they are the expected
conservativity), false negatives are not.
"""

import random
import unittest

from tests.diff_engine import run_case


def gen_expr(rng, vars_, depth=0):
    if depth > 2 or rng.random() < 0.4:
        kind = rng.choice(["var", "const", "arr"])
        if kind == "var" and vars_:
            return rng.choice(vars_)
        if kind == "arr":
            return f"a[{gen_expr(rng, vars_, depth + 1)}]"
        return str(rng.randint(-2, 3))
    op = rng.choice(["+", "-", "*", "+", "-", "/", "%"])
    l = gen_expr(rng, vars_, depth + 1)
    r = gen_expr(rng, vars_, depth + 1)
    return f"({l} {op} {r})"


def gen_program(rng):
    two = rng.random() < 0.5
    inputs = ["x"] + (["y"] if two else [])
    lines = ["input x;"]
    if two:
        lines.append("input y;")
    lines.append("arr a[3] = [0, 1, 2];")
    # always declare the temporaries statements may assign to
    lines.append("var t0 = 0;")
    lines.append("var t1 = 0;")
    lines.append("var t2 = 0;")
    lines.append("var i = 0;")
    vars_ = list(inputs) + ["t0", "t1", "t2", "i"]

    # a few scalar initializations from expressions
    for k in range(rng.randint(1, 3)):
        t = f"t{rng.randint(0, 2)}"
        lines.append(f"{t} = {gen_expr(rng, vars_)};")

    def some_stmts(n):
        out = []
        for _ in range(n):
            j = rng.randint(0, 4)
            if j == 0:
                out.append(f"a[{gen_expr(rng, vars_)}] = {gen_expr(rng, vars_)};")
            elif j == 1:
                t = f"t{rng.randint(0, 2)}"
                out.append(f"{t} = {gen_expr(rng, vars_)};")
            elif j == 2:
                # guarded division: divide only when the divisor is nonzero
                d = gen_expr(rng, vars_)
                out.append(f"if (({d}) != 0) {{ t0 = 10 / ({d}); }}")
            elif j == 3:
                out.append(f"t0 = {gen_expr(rng, vars_)};")
            else:
                out.append(f"t1 = a[{gen_expr(rng, vars_)}];")
        return out

    lines.extend(some_stmts(rng.randint(2, 5)))

    # constant-trip-count loop (always terminates) with a computed index
    if rng.random() < 0.7:
        hi = rng.randint(0, 4)
        lines.append("i = 0;")
        lines.append(f"while (i < {hi}) {{")
        # index expression mixing i and an input — exercises negative/oob
        idx = rng.choice(["i", "i - 1", "i + x", "x", "i - x"])
        lines.append(f"  a[{idx}] = i;")
        if rng.random() < 0.5:
            lines.append(f"  t0 = a[{idx}];")
        lines.append("  i = i + 1;")
        lines.append("}")

    return "\n".join(lines) + "\n"


class FuzzSoundnessTests(unittest.TestCase):
    def test_random_programs_are_sound(self):
        n_programs = 1500
        ranges = {"x": (-2, 2), "y": (-2, 2)}
        failures = []
        fp_programs = 0
        total_runs = 0
        valid = 0
        for seed_i in range(n_programs):
            sub_rng = random.Random(20260923 + seed_i)
            src = gen_program(sub_rng)
            case = {
                "name": f"fuzz#{seed_i}",
                "source": src,
                "ranges": dict(ranges),
                "precision": "imprecise",
                "expected_false_alarms": [],
                "require_all_documented": False,
            }
            r = run_case(case)
            if r.get("malformed"):
                continue
            valid += 1
            total_runs += r["runs"]
            if r["false_alarms"]:
                fp_programs += 1
            if r["missed"]:
                failures.append((case["name"], src, r["missed"]))

        print(f"\n[fuzz] {valid}/{n_programs} valid generated programs, "
              f"{total_runs} concrete runs, "
              f"{fp_programs} programs with conservative (false) alarms, "
              f"{len(failures)} unsound")
        if failures:
            name, src, missed = failures[0]
            self.fail(f"UNSOUND {name}: missed {missed}\n{src}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
