"""Property-style differential fuzzing: randomly generated valid-ish programs
must produce identical traces in the source and IR interpreters.

The generator is deliberately small but covers nested functions, parameters,
shadowing, loops, captured mutable counters and multiple closures sharing
state.
"""

import random
import unittest

from tests.test_differential import run_both


def make_counter_program(seed: int) -> str:
    rng = random.Random(seed)
    n_ops = rng.randint(1, 8)
    ops = []
    for _ in range(n_ops):
        ops.append(rng.choice(["p(0)", "p(1)", "p(2)"]))
    body = "\n".join(f"  print({op});" for op in ops)
    return f"""
fn pair(start) {{
  let n = start;
  let inc = fn() {{ n = n + 1; return n; }};
  let dec = fn() {{ n = n - 1; return n; }};
  let get = fn() {{ return n; }};
  return fn(op) {{
    if (op == 0) {{ return inc(); }}
    if (op == 1) {{ return dec(); }}
    return get();
  }};
}}
let p = pair({rng.randint(-5, 5)});
{body}
"""


def make_shadow_program(seed: int) -> str:
    rng = random.Random(seed)
    lines = ["let v = 0;"]
    depth = 0
    declared_at_depth = {0: True}   # depth 0 already declares v
    for _ in range(rng.randint(3, 10)):
        indent = "  " * (depth + 1)
        choice = rng.choice(["shadow", "read", "write", "open", "close"])
        if choice == "open" and depth < 3:
            lines.append("  " * (depth + 1) + "{")
            depth += 1
            declared_at_depth[depth] = False
        elif choice == "close" and depth > 0:
            lines.append("  " * depth + "}")
            depth -= 1
        elif choice == "shadow" and depth > 0 and not declared_at_depth.get(depth, False):
            lines.append(indent + f"let v = {rng.randint(0, 99)};")
            lines.append(indent + "print(v);")
            declared_at_depth[depth] = True
        elif choice == "write":
            lines.append(indent + f"v = {rng.randint(0, 99)};")
            lines.append(indent + "print(v);")
        else:
            lines.append(indent + "print(v);")
    while depth > 0:
        lines.append("  " * depth + "}")
        depth -= 1
    lines.append("print(v);")
    return "\n".join(lines)


def make_recursive_program(seed: int) -> str:
    rng = random.Random(seed)
    n = rng.randint(0, 12)
    return f"""
fn loop_sum(n, acc) {{
  if (n == 0) {{ return acc; }}
  return loop_sum(n - 1, acc + n);
}}
print(loop_sum({n}, 0));
"""


class FuzzDifferentialTests(unittest.TestCase):
    def test_counters(self):
        for seed in range(40):
            with self.subTest(seed=seed):
                s, i = run_both(make_counter_program(seed))
                self.assertEqual(s, i)

    def test_shadow_programs(self):
        for seed in range(40):
            with self.subTest(seed=seed):
                src = make_shadow_program(seed)
                s, i = run_both(src)
                self.assertEqual(s, i)

    def test_recursion(self):
        for seed in range(30):
            with self.subTest(seed=seed):
                s, i = run_both(make_recursive_program(seed))
                self.assertEqual(s, i)


if __name__ == "__main__":
    unittest.main()
