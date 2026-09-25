"""Random differential testing: generate LattLang programs, run them through
AST / SSA IR / optimized IR, and assert identical observable behavior.

The generator is deliberately structured so every generated program
terminates quickly: each loop gets a fresh, body-inaccessible counter
variable (``_c0``, ``_c1``, ...) initialized to a small constant and only
incremented at the end of its own body.  This exercises phi nodes, branch
folding and the division safety rules without risking non-termination.
"""

import random
import re
import unittest

from lattlang.interp_ast import run_ast
from lattlang.interp_ir import run_ir
from lattlang.irbuild import build_ir
from lattlang.optimize import optimize
from lattlang.parser import parse
from lattlang.analysis import run_sccp
from lattlang import ssa as ssa_mod

VARS = ["x", "i", "a", "b"]
ARITH_OPS = ["+", "-", "*"]
DIV_OPS = ["/", "%"]
COMPARE_OPS = ["==", "!=", "<", ">", "<=", ">="]
LOGIC_OPS = ["&&", "||"]


class ProgramGenerator:
    def __init__(self, rng: random.Random):
        self.rng = rng
        self.indent = 0
        self.loop_seq = 0

    def line(self, text: str) -> str:
        return "  " * self.indent + text

    def counter(self) -> str:
        # Identifiers that user code never generates -> reserved counters.
        self.loop_seq += 1
        return f"_c{self.loop_seq}"

    def expr(self, depth: int = 0) -> str:
        if depth > 3 or self.rng.random() < 0.3:
            return str(self.rng.randint(0, 6))
        if self.rng.random() < 0.25:
            return self.rng.choice(VARS)
        if self.rng.random() < 0.1:
            return "-(" + self.expr(depth + 1) + ")"
        bucket = self.rng.choices(
            [ARITH_OPS, DIV_OPS, COMPARE_OPS, LOGIC_OPS],
            weights=[4, 2, 2, 2], k=1)[0]
        op = self.rng.choice(bucket)
        return f"({self.expr(depth + 1)} {op} {self.expr(depth + 1)})"

    def stmt(self, depth: int, allow_loop: bool = True) -> list[str]:
        choices = ["assign", "print", "if"]
        weights = [3, 3, 3 if depth < 3 else 0]
        if allow_loop and depth < 3:
            choices.append("while")
            weights.append(2)
        kind = self.rng.choices(choices, weights=weights, k=1)[0]

        if kind == "assign":
            return [self.line(f"{self.rng.choice(VARS)} := {self.expr()};")]
        if kind == "print":
            return [self.line(f"print {self.expr()};")]
        if kind == "if":
            lines = [self.line(f"if {self.expr()} {{")]
            self.indent += 1
            for _ in range(self.rng.randint(0, 3)):
                lines += self.stmt(depth + 1, allow_loop)
            self.indent -= 1
            lines.append(self.line("} else {"))
            self.indent += 1
            for _ in range(self.rng.randint(0, 3)):
                lines += self.stmt(depth + 1, allow_loop)
            self.indent -= 1
            lines.append(self.line("}"))
            return lines

        # bounded while loop with a fresh, body-scoped counter
        ctr = self.counter()
        bound = self.rng.randint(0, 5)
        lines = [self.line(f"{ctr} := 0;"),
                 self.line(f"while {ctr} < {bound} {{")]
        self.indent += 1
        # body may contain if/print/assign and nested loops, but never
        # assigns to ctr (VARS excludes the _cN counters)
        for _ in range(self.rng.randint(0, 3)):
            lines += self.stmt(depth + 1, allow_loop=True)
        lines.append(self.line(f"{ctr} := {ctr} + 1;"))
        self.indent -= 1
        lines.append(self.line("}"))
        return lines

    def program(self) -> str:
        lines = []
        for _ in range(self.rng.randint(1, 8)):
            lines += self.stmt(0)
        return "\n".join(lines)


_COUNTER_RE = re.compile(r"_c\d+")


def sig(r):
    return (tuple(r.output),
            None if r.ok else (r.error.stage, r.error.message,
                               r.error.span.offset if r.error.span else None))


def run_all(src: str):
    program = parse(src)
    cfg = build_ir(program)
    ssa_prog = ssa_mod.build_ssa(cfg)
    opt_prog = optimize(ssa_prog, run_sccp(ssa_prog)).program
    return (run_ast(program, max_steps=100_000),
            run_ir(ssa_prog, max_steps=100_000),
            run_ir(opt_prog, max_steps=100_000))


class TestFuzz(unittest.TestCase):
    def test_random_programs_equivalent(self):
        rng = random.Random(20260923)
        failures = []
        for trial in range(250):
            src = ProgramGenerator(rng).program()
            # generator invariant: counters only appear in their own loop
            try:
                ra, ri, ro = run_all(src)
            except Exception as e:
                failures.append(("pipeline", src, repr(e)))
                continue
            sa, si, so = sig(ra), sig(ri), sig(ro)
            if not (sa == si == so):
                failures.append(("behavior", src, (sa, si, so)))
        if failures:
            msg = ["", f"{len(failures)} mismatching program(s); first 3:"]
            for kind, src, detail in failures[:3]:
                msg.append(f"--- [{kind}]\n{src}\n=> {detail}")
            self.fail("\n".join(msg))

    def test_generator_uses_reserved_counters(self):
        src = ProgramGenerator(random.Random(7)).program()
        # _cN appears 4 times: init, condition, increment LHS, increment RHS
        for ctr in set(_COUNTER_RE.findall(src)):
            self.assertEqual(len(re.findall(rf"\b{re.escape(ctr)}\b", src)), 4,
                             f"{ctr} misuse in:\n{src}")

    def test_generator_determinism(self):
        a = ProgramGenerator(random.Random(1)).program()
        b = ProgramGenerator(random.Random(1)).program()
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
