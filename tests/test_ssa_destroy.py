"""Phi elimination: critical edges, parallel copies, cycles."""
from __future__ import annotations

import unittest

from ssa_toolchain.analysis.ssa_construct import construct_ssa
from ssa_toolchain.analysis.ssa_destroy import (eliminate_phis,
                                                _sequence_parallel_copies)
from ssa_toolchain.analysis.verify import verify_ssa
from ssa_toolchain.interp import Interpreter
from ssa_toolchain.ir import Module
from ssa_toolchain.ir_parser import parse_ir
from ssa_toolchain.parser import parse
from ssa_toolchain.frontend.builder import build_module

from .helpers import compile_source, load_example


class ParallelCopySequencingTests(unittest.TestCase):
    def test_plain_chain(self):
        pairs = [("%a", "%x"), ("%b", "%a"), ("%c", "%b")]
        # The scheduler may emit straight through (no temporaries);
        # simulate and assert the parallel-copy semantics instead of a
        # fixed textual order.
        instrs, temps, _, _ = _sequence_parallel_copies(
            pairs, 0, set())
        self.assertEqual(temps, 0)
        env = {"%x": 99}
        for i in instrs:
            env[i.dest] = env[i.operands[0]]
        self.assertEqual((env["%a"], env["%b"], env["%c"]),
                         (99, 99, 99))

    def test_fan_out(self):
        pairs = [("%a", "%x"), ("%b", "%x"), ("%c", "%x")]
        instrs, temps, _, _ = _sequence_parallel_copies(
            pairs, 0, set())
        self.assertEqual(temps, 0)
        env = {"%x": 5}
        for i in instrs:
            env[i.dest] = env[i.operands[0]]
        self.assertEqual((env["%a"], env["%b"], env["%c"]), (5, 5, 5))

    def test_swap_cycle_uses_one_temp(self):
        pairs = [("%a", "%b"), ("%b", "%a")]
        instrs, temps, _, _ = _sequence_parallel_copies(
            pairs, 0, set())
        self.assertEqual(temps, 1)
        env = {"%a": 1, "%b": 7}
        for i in instrs:
            env[i.dest] = env[i.operands[0]]
        self.assertEqual((env["%a"], env["%b"]), (7, 1))

    def test_three_cycle(self):
        pairs = [("%a", "%b"), ("%b", "%c"), ("%c", "%a")]
        instrs, temps, _, _ = _sequence_parallel_copies(
            pairs, 0, set())
        self.assertEqual(temps, 1)
        env = {"%a": 1, "%b": 2, "%c": 3}
        for i in instrs:
            env[i.dest] = env[i.operands[0]]
        self.assertEqual((env["%a"], env["%b"], env["%c"]),
                         (2, 3, 1))

    def test_cycle_with_in_tree(self):
        # x <- a, d <- x feed the a/b cycle; d must receive old x and x
        # must receive old a.
        pairs = [("%a", "%b"), ("%b", "%a"),
                 ("%d", "%x"), ("%x", "%a")]
        instrs, temps, _, _ = _sequence_parallel_copies(
            pairs, 0, set())
        self.assertGreaterEqual(temps, 1)
        env = {"%a": 1, "%b": 2, "%x": 9}
        for i in instrs:
            env[i.dest] = env[i.operands[0]]
        self.assertEqual(env["%d"], 9)
        self.assertEqual(env["%x"], 1)
        self.assertEqual((env["%a"], env["%b"]), (2, 1))

    def test_cycle_with_chained_in_tree(self):
        # y <- x <- a feeds the cycle: y keeps old x, x gets old a.
        pairs = [("%a", "%b"), ("%b", "%a"),
                 ("%x", "%a"), ("%y", "%x")]
        instrs, temps, _, _ = _sequence_parallel_copies(
            pairs, 0, set())
        self.assertGreaterEqual(temps, 1)
        env = {"%a": 1, "%b": 2, "%x": 0}
        for i in instrs:
            env[i.dest] = env[i.operands[0]]
        self.assertEqual((env["%a"], env["%b"]), (2, 1))
        self.assertEqual(env["%x"], 1)
        self.assertEqual(env["%y"], 0)


class SwapLoopEndToEndTests(unittest.TestCase):
    def test_backedge_cycle_broken(self):
        res = compile_source(load_example("swap_loop.mini"), inputs=[3])
        fp = res.funcs["main"]
        self.assertGreaterEqual(fp.swap_temps, 1)
        body = fp.flat.blocks["b.while1.body"]
        # a swap temporary must appear before a/b are rewritten
        self.assertTrue(any(i.dest and i.dest.startswith("%cswap")
                            for i in body.instrs))

    def test_even_odd_iterations(self):
        for n, want in [(0, (1, 7)), (1, (7, 1)), (2, (1, 7)),
                        (3, (7, 1)), (4, (1, 7))]:
            res = compile_source(load_example("swap_loop.mini"), inputs=[n])
            ex = res.executions["main"]
            self.assertTrue(ex["agree"], msg=f"n={n}")
            self.assertEqual(ex["flat"]["printed"], list(want),
                             msg=f"n={n}")
            self.assertEqual(ex["flat"]["return"], 8)


class CriticalEdgeHandWrittenTests(unittest.TestCase):
    IR = """
    func main(%p0) {
    entry:
      %c1 = const 1
      %c2 = const 2
      br %p0, side, join(%c1)
    side:
      br %p0, join(%c2), exit
    join:
      %x.phi0 = phi [entry %c1] [side %c2]
      ret %x.phi0
    exit:
      ret %c1
    }
    """

    def test_edges_split_and_executes(self):
        m = parse_ir(self.IR)
        verify_ssa(m.funcs["main"])
        r = eliminate_phis(m.funcs["main"])
        self.assertEqual(len(r.split_edges), 2)
        flat = Module()
        flat.add_function(r.func)
        # input 0: entry(false)->side; side(false)->exit => 1
        # input 1: entry(false)->side; side(true)->join with 2 => 2
        self.assertEqual(Interpreter(flat, "flat").run("main", [0]).return_value, 1)
        self.assertEqual(Interpreter(flat, "flat").run("main", [1]).return_value, 2)

    def test_no_phis_remain(self):
        m = parse_ir(self.IR)
        r = eliminate_phis(m.funcs["main"])
        for b in r.func.blocks.values():
            self.assertEqual(b.phis, [])
            self.assertFalse(b.term.args or b.term.args_t or b.term.args_f)


class StructuredProgramsNeedNoSplitTests(unittest.TestCase):
    def test_standard_examples_have_no_critical_edges(self):
        for name in ("diamond", "sum_loop", "swap_loop", "nested"):
            res = compile_source(load_example(name + ".mini"), inputs=[3])
            self.assertEqual(res.funcs["main"].split_edges, [],
                             msg=name)


if __name__ == "__main__":
    unittest.main()
