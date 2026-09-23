"""SSA construction: phi insertion, renaming, pruned unreachable blocks."""
from __future__ import annotations

import unittest

from ssa_toolchain.analysis.ssa_construct import construct_ssa
from ssa_toolchain.analysis.verify import verify_ssa
from ssa_toolchain.interp import Interpreter
from ssa_toolchain.ir import Module
from ssa_toolchain.parser import parse
from ssa_toolchain.frontend.builder import build_module

from .helpers import compile_source, load_example


def build_raw(src):
    prog = parse(src)
    names = {f.name: list(f.params) for f in prog.funcs}
    m, _ = build_module(prog)
    return m, names


class DiamondSSATests(unittest.TestCase):
    def setUp(self):
        self.res = compile_source(load_example("diamond.mini"), inputs=[5])
        self.fp = self.res.funcs["main"]

    def test_one_phi_at_join(self):
        self.assertEqual(self.fp.phi_blocks,
                         {"x": ["b.if1.merge"]})
        merge = self.fp.ssa.blocks["b.if1.merge"]
        self.assertEqual(len(merge.phis), 1)
        self.assertEqual(merge.phis[0].var, "x")

    def test_every_ssa_name_defined_once(self):
        # verify_ssa raises on duplicates; call it explicitly
        self.assertEqual(verify_ssa(self.fp.ssa)[0],
                         "single-definition ok")

    def test_incoming_values_trace_to_branches(self):
        phi = self.fp.ssa.blocks["b.if1.merge"].phis[0]
        rows = dict(phi.incoming)
        self.assertEqual(set(rows), {"b.if1.then", "b.if1.else"})

    def test_execution_matches_for_many_inputs(self):
        for n in range(-10, 11):
            res = compile_source(load_example("diamond.mini"), inputs=[n])
            ex = res.executions["main"]
            self.assertTrue(ex["agree"])
            self.assertEqual(ex["flat"]["return"], n + 1 if n > 0 else n - 1)


class LoopCarriedSSATests(unittest.TestCase):
    def test_header_phis_for_sum_and_i(self):
        res = compile_source(load_example("sum_loop.mini"), inputs=[5])
        fp = res.funcs["main"]
        header = fp.ssa.blocks["b.while1.cond"]
        phi_vars = {p.var for p in header.phis}
        self.assertEqual(phi_vars, {"sum", "i"})
        for phi in header.phis:
            preds = {p for p, _ in phi.incoming}
            self.assertEqual(preds, {"entry", "b.while1.body"})

    def test_sum_1_to_10(self):
        res = compile_source(load_example("sum_loop.mini"), inputs=[10])
        ex = res.executions["main"]
        self.assertTrue(ex["agree"])
        self.assertEqual(ex["flat"]["return"], 55)

    def test_zero_iterations(self):
        res = compile_source(load_example("sum_loop.mini"), inputs=[0])
        self.assertEqual(res.executions["main"]["flat"]["return"], 0)


class UnreachablePruneTests(unittest.TestCase):
    def setUp(self):
        self.res = compile_source(load_example("unreachable.mini"),
                                  inputs=[0])
        self.fp = self.res.funcs["main"]

    def test_dead_blocks_reported_and_removed(self):
        self.assertIn("b.if1.else", self.fp.removed)
        self.assertIn("b.while1.cond", self.fp.removed)
        self.assertIn("b.while1.body", self.fp.removed)
        for label in self.fp.removed:
            self.assertNotIn(label, self.fp.ssa.blocks)

    def test_phi_only_for_live_join(self):
        # x's merge is reachable from the taken side only: after pruning
        # the join has a single predecessor, so no phi is needed.
        self.assertNotIn("x", self.fp.phi_blocks)

    def test_result_is_20(self):
        ex = self.res.executions["main"]
        self.assertTrue(ex["agree"])
        self.assertEqual(ex["flat"]["return"], 20)
        self.assertEqual(ex["flat"]["printed"], [20, 0])


class VarInsideLoopTests(unittest.TestCase):
    """Declaration inside the loop body must still produce a header phi."""

    SRC = """
    func main(n) {
      var i = 0;
      var sum = 0;
      while (i < n) {
        var v = i * 2;
        sum = sum + v;
        i = i + 1;
      }
      return sum;
    }
    """

    def test_phi_for_body_local(self):
        res = compile_source(self.SRC, inputs=[4])
        header = res.funcs["main"].ssa.blocks["b.while1.cond"]
        self.assertEqual({p.var for p in header.phis}, {"i", "sum", "v"})
        self.assertEqual(res.executions["main"]["flat"]["return"],
                         2 * (0 + 1 + 2 + 3))


class NoPhiWhenNotNeededTests(unittest.TestCase):
    def test_straight_line_has_no_phi(self):
        src = "func main() { var x = 1; x = x + 2; return x; }"
        res = compile_source(src, inputs=[])
        self.assertEqual(res.funcs["main"].phi_blocks, {})


if __name__ == "__main__":
    unittest.main()
