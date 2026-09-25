import unittest

from lattlang.interp_ir import run_ir
from lattlang.irbuild import build_ir
from lattlang.optimize import optimize
from lattlang.parser import parse
from lattlang.analysis import run_sccp
from lattlang import ssa as ssa_mod


def optimize_src(src):
    ssa_prog = ssa_mod.build_ssa(build_ir(parse(src)))
    report = optimize(ssa_prog, run_sccp(ssa_prog))
    return report


def iter_text(prog):
    out = []
    for b in prog.blocks:
        for ins in b.instrs:
            out.append((ins.kind, ins.op,
                        ins.args[1].value if len(ins.args) > 1
                        and hasattr(ins.args[1], "value") else None))
    return out


class TestOptimize(unittest.TestCase):
    def test_constant_branch_drops_dead_block(self):
        rep = optimize_src("if 1 { print 1; } else { print 2; }")
        labels = {b.label for b in rep.program.blocks}
        self.assertFalse(any("else" in l for l in labels))
        self.assertTrue(any(c["kind"] == "fold_branch" for c in rep.changes))

    def test_reachable_divzero_preserved(self):
        rep = optimize_src("if 1 { print 1 / 0; } else { print 2; }")
        divs = [(b.label, i) for b in rep.program.blocks for i in b.instrs
                if i.kind == "binary" and i.op == "div"]
        self.assertEqual(len(divs), 1)
        block, div = divs[0]
        self.assertEqual(div.args[1].value, 0)
        r = run_ir(rep.program)
        self.assertIsNotNone(r.error)
        self.assertEqual(r.error.message, "division or modulo by zero")

    def test_unreachable_divzero_removed_with_block(self):
        rep = optimize_src("if 0 { print 1 / 0; }\nprint 5;")
        for b in rep.program.blocks:
            for i in b.instrs:
                if i.kind == "binary":
                    self.assertNotEqual(i.op, "div")
        r = run_ir(rep.program)
        self.assertTrue(r.ok)
        self.assertEqual(r.output, ["5"])

    def test_dynamic_divisor_op_retained(self):
        src = "x := 0;\nwhile x < 3 { print 10 / x; x := x + 1; }"
        rep = optimize_src(src)
        divs = [i for b in rep.program.blocks for i in b.instrs
                if i.kind == "binary" and i.op == "div"]
        self.assertEqual(len(divs), 1)
        r = run_ir(rep.program)
        self.assertIsNotNone(r.error)  # traps on first iteration (10/0)

    def test_proven_nonzero_divisor_can_fold_away(self):
        # divisor is a proven constant 2 -> pure, may be folded/DCE'd
        rep = optimize_src("print 10 / 2;")
        r = run_ir(rep.program)
        self.assertEqual(r.output, ["5"])

    def test_print_ordering_preserved(self):
        src = ("if 1 { print 3; print 4; } else { print 100; }\nprint 9;")
        r = run_ir(optimize_src(src).program)
        self.assertEqual(r.output, ["3", "4", "9"])

    def test_dead_print_in_removed_block_disappears(self):
        rep = optimize_src("if 0 { print 100; }")
        prints = [i for b in rep.program.blocks for i in b.instrs
                  if i.kind == "print"]
        self.assertEqual(prints, [])

    def test_loop_with_runtime_trip_not_killed(self):
        src = "i := 0;\nwhile i < 3 { print i; i := i + 1; }\nprint 99;"
        rep = optimize_src(src)
        r = run_ir(rep.program)
        self.assertEqual(r.output, ["0", "1", "2", "99"])

    def test_constants_substituted_as_immediates(self):
        rep = optimize_src("x := 2;\ny := x * 3;\nprint y;")
        text_ops = [(i.kind, i.op) for b in rep.program.blocks
                    for i in b.instrs if i.dest]
        # mul should be folded; final print carries immediate 6
        prints = [i for b in rep.program.blocks for i in b.instrs
                  if i.kind == "print"]
        self.assertEqual(prints[0].args[0].value, 6)
        self.assertFalse(any(op == "mul" for _, op in text_ops))

    def test_phi_rebuilt_after_branch_fold(self):
        # branch folded away; the merge phi must end with a single live arg
        src = "if 1 { x := 1; } else { x := 2; }\nprint x;"
        rep = optimize_src(src)
        r = run_ir(rep.program)
        self.assertEqual(r.output, ["1"])


if __name__ == "__main__":
    unittest.main()
