"""Property-style tests: random structured programs must agree across
memory-mode execution of the original IR and flat-mode execution of the
phi-eliminated IR, and every SSA value must verify as single-assignment.
"""
from __future__ import annotations

import random
import unittest

from ssa_toolchain.analysis.verify import verify_ssa
from ssa_toolchain.interp import Interpreter

from .helpers import compile_source


class RandomProgramGenerator:
    """Generates straight-line + nested if/while programs over a small
    fixed variable pool so every program stays valid and terminating."""

    def __init__(self, rng: random.Random, depth: int = 3):
        self.rng = rng
        self.depth = depth
        self.var_i = 0
        self.lines: list[str] = []

    def fresh(self) -> str:
        self.var_i += 1
        return f"v{self.var_i}"

    def expr(self, atoms: list[str]) -> str:
        ops = ["+", "-", "*", "&", "|", "^", "==", "!=", "<", "<=", ">"]
        kind = self.rng.randrange(4)
        if kind == 0 or not atoms:
            return str(self.rng.randrange(-5, 6))
        if kind == 1:
            return self.rng.choice(atoms)
        op = self.rng.choice(ops)
        a = self.rng.choice(atoms) if atoms else "1"
        return f"({a} {op} {self.rng.randrange(0, 5)})"

    def block(self, atoms, depth, loop_budget, frozen=None):
        frozen = frozen or set()
        for _ in range(self.rng.randrange(1, 4)):
            choice = self.rng.randrange(3 if depth > 0 else 1)
            if choice == 0:
                name = self.fresh()
                self.lines.append(
                    f"var {name} = {self.expr(atoms)};")
                atoms = atoms + [name]
            elif choice == 1 and atoms:
                target = self.rng.choice(atoms)
                if target in frozen:
                    continue
                self.lines.append(
                    f"{target} = {self.expr(atoms)};")
            elif choice == 2 and depth > 0:
                if self.rng.random() < 0.5:
                    self.lines.append(f"if ({self.expr(atoms)}) {{")
                    self.block(atoms, depth - 1, loop_budget, frozen)
                    if self.rng.random() < 0.6:
                        self.lines.append("} else {")
                        self.block(atoms, depth - 1, loop_budget, frozen)
                    self.lines.append("}")
                elif loop_budget > 0:
                    counter = self.fresh()
                    lim = self.rng.randrange(0, 4)
                    self.lines.append(f"var {counter} = 0;")
                    # the loop body may read the counter but must not
                    # assign it, or the trip count becomes unbounded.
                    atoms2 = atoms + [counter]
                    self.lines.append(f"while ({counter} < {lim}) {{")
                    self.block(atoms2, depth - 1, loop_budget - 1,
                               frozen=frozen | {counter})
                    self.lines.append(f"{counter} = {counter} + 1;")
                    self.lines.append("}")
        return atoms

    def program(self) -> str:
        atoms = ["n"]
        self.block(atoms, self.depth, loop_budget=2)
        result_var = None
        # return a defined var if any, else a constant
        body = "\n  ".join(self.lines)
        return (f"func main(n) {{\n  {body}\n"
                f"  return n + {self.rng.randrange(0, 3)};\n}}\n")


class FuzzEquivalenceTests(unittest.TestCase):
    TRIALS = 60

    def test_random_programs_agree(self):
        rng = random.Random(20260923)
        for trial in range(self.TRIALS):
            gen = RandomProgramGenerator(rng, depth=3)
            src = gen.program()
            n = rng.randrange(-3, 4)
            try:
                res = compile_source(src, inputs=[n])
            except Exception as e:  # pragma: no cover - diagnostics
                self.fail(f"trial {trial} failed to compile:\n{src}\n{e!r}")
            fp = res.funcs["main"]
            verify_ssa(fp.ssa)  # single definition + dominance
            ex = res.executions["main"]
            self.assertTrue(ex["agree"],
                            msg=f"trial {trial} n={n}\n{src}\n"
                                f"memory={ex['memory']} flat={ex['flat']}")


if __name__ == "__main__":
    unittest.main()
