"""Randomized differential fuzzing.

Generates straight-line programs over a few input variables with random
arithmetic and randomly inserted guards, then exhaustively executes every
point in a bounded input region and asserts the soundness contract:

  * no concrete division-by-zero / out-of-bounds site is missed;
  * every terminating final value is covered by an abstract exit interval.

The generated programs only use fixed-length arrays and indices that are
linear in the bounded inputs, so enumeration is complete over the region.
"""

import random
import unittest

from interval_ai.diffcheck import exhaustive_check, exhaustive_value_check


def generate_program(rng: random.Random) -> tuple[str, dict]:
    n_inputs = rng.randint(1, 3)
    names = [f"x{i}" for i in range(n_inputs)]
    decls = "\n".join(f"input var {n};" for n in names)
    body_lines = []
    tmp_count = rng.randint(0, 3)
    for t in range(tmp_count):
        op = rng.choice(["+", "-", "*"])
        a = rng.choice(names + [f"t{j}" for j in range(t)] or names)
        b = rng.choice(names + [str(rng.randint(-2, 3))])
        body_lines.append(f"var t{t} = {a} {op} {b};")
    if rng.random() < 0.8:
        x = rng.choice(names)
        k = rng.randint(1, 4)
        body_lines.append(f"if ({x} >= {k}) {{ var d = 100 / {x}; }}")
    if rng.random() < 0.5:
        x = rng.choice(names)
        k = rng.randint(-4, -1)
        body_lines.append(f"if ({x} <= {k}) {{ var e = 100 / ({x}); }}")
    if rng.random() < 0.5:
        x = rng.choice(names)
        body_lines.append(f"var u = 7 / ({x} + {rng.randint(0, 2)});")
    src = decls + "\n" + "\n".join(body_lines) + "\n"
    region = {n: (-4, 4) for n in names}
    return src, region


def generate_array_program(rng: random.Random) -> tuple[str, dict]:
    n_inputs = rng.randint(1, 2)
    names = [f"i{j}" for j in range(n_inputs)]
    length = rng.choice([3, 4, 5])
    decls = f"input array arr[{length}];\n" + "\n".join(
        f"input var {n};" for n in names)
    lines = []
    x = rng.choice(names)
    style = rng.choice(["unguarded", "guarded", "constant_neg", "bounded_add"])
    if style == "unguarded":
        lines.append(f"arr[{x}] = 1;")
    elif style == "guarded":
        lines.append(f"if ({x} >= 0) {{")
        lines.append(f"  if ({x} < {length}) {{ arr[{x}] = 1; }}")
        lines.append("}")
    elif style == "constant_neg":
        lines.append("arr[-2] = 1;")
    else:
        c = rng.randint(0, length)
        lines.append(f"arr[{x} + {c}] = 1;")
    src = decls + "\n" + "\n".join(lines) + "\n"
    region = {n: (-length - 2, length + 2) for n in names}
    return src, region


def generate_loop_program(rng: random.Random) -> tuple[str, dict]:
    """A bounded (and sometimes unknown-bound) counting loop that indexes an
    array, optionally nested -- stresses widening/narrowing soundness."""
    length = rng.choice([4, 6])
    use_unknown = rng.random() < 0.5
    bound_name = "n"
    decls = f"input array arr[{length}];\n"
    if use_unknown:
        decls += "input var n;\n"
        bound_hi = length + 2
        region = {"n": (-1, bound_hi)}
    else:
        bconst = rng.randint(1, length)
        decls += f"var n = {bconst};\n"
        region = {}
    nested = rng.random() < 0.5
    lines = ["var i = 0;"]
    if nested:
        lines.append("while (i < n) {")
        lines.append("  var j = 0;")
        lines.append("  while (j < n) {")
        lines.append(f"    arr[j] = j;")
        lines.append("    j = j + 1;")
        lines.append("  }")
        lines.append("  i = i + 1;")
        lines.append("}")
    else:
        lines.append("while (i < n) {")
        lines.append("  arr[i] = i;")
        lines.append("  i = i + 1;")
        lines.append("}")
    src = decls + "\n" + "\n".join(lines) + "\n"
    return src, region


class TestFuzz(unittest.TestCase):
    def test_random_scalar_programs_sound(self):
        rng = random.Random(20260923)
        for trial in range(80):
            src, region = generate_program(rng)
            with self.subTest(trial=trial, src=src):
                rep = exhaustive_check(src, region, step_limit=50_000)
                self.assertEqual(rep.missed, [],
                                 msg=f"missed crash for:\n{src}")
                vrep = exhaustive_value_check(src, region,
                                              step_limit=50_000)
                self.assertEqual(vrep.violations, [],
                                 msg=f"invariant violation:\n{src}\n"
                                     f"{vrep.violations[:3]}")

    def test_random_array_programs_sound(self):
        rng = random.Random(987654321)
        for trial in range(60):
            src, region = generate_array_program(rng)
            with self.subTest(trial=trial, src=src):
                rep = exhaustive_check(src, region, step_limit=200_000)
                self.assertEqual(rep.missed, [],
                                 msg=f"missed OOB for:\n{src}")
                vrep = exhaustive_value_check(src, region,
                                              step_limit=200_000)
                self.assertEqual(vrep.violations, [],
                                 msg=f"invariant violation:\n{src}")

    def test_random_loop_programs_sound(self):
        rng = random.Random(13579)
        for trial in range(40):
            src, region = generate_loop_program(rng)
            with self.subTest(trial=trial, src=src):
                rep = exhaustive_check(src, region, step_limit=300_000)
                self.assertEqual(rep.missed, [],
                                 msg=f"missed crash for:\n{src}")
                vrep = exhaustive_value_check(src, region,
                                              step_limit=300_000)
                self.assertEqual(vrep.violations, [],
                                 msg=f"invariant violation:\n{src}\n"
                                     f"{vrep.violations[:3]}")


if __name__ == "__main__":
    unittest.main()
