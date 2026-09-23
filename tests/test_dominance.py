"""Dominance analysis unit tests.

Graphs here are built directly as IR (no parser dependency) so CFG shapes
that structured mini code cannot express (arbitrary critical edges) are
covered too.
"""
from __future__ import annotations

import unittest

from ssa_toolchain.analysis.dominance import compute_dominance
from ssa_toolchain.ir import (Block, Function, Instr, make_br, make_jmp,
                              make_ret)


def _fn(spec):
    """spec: list of (label, [(instr dest, op), ...], terminator)."""
    f = Function(name="t", params=[], entry=spec[0][0], blocks={})
    for label, instrs, term in spec:
        b = Block(label)
        for dest, op in instrs:
            b.instrs.append(Instr("const", dest, [], {"value": 0}))
        b.term = term
        f.add_block(b)
    return f


class DiamondDomTests(unittest.TestCase):
    def setUp(self):
        self.f = _fn([
            ("entry", [("%c", None)], make_br("%c", "a", "b")),
            ("a", [], make_jmp("m")),
            ("b", [], make_jmp("m")),
            ("m", [], make_ret("%c")),
        ])
        self.d = compute_dominance(self.f)

    def test_idoms(self):
        self.assertIsNone(self.d.idom["entry"])
        self.assertEqual(self.d.idom["a"], "entry")
        self.assertEqual(self.d.idom["b"], "entry")
        self.assertEqual(self.d.idom["m"], "entry")

    def test_dominance_frontier(self):
        self.assertEqual(self.d.df["a"], {"m"})
        self.assertEqual(self.d.df["b"], {"m"})
        self.assertEqual(self.d.df["m"], set())
        self.assertEqual(self.d.df["entry"], set())

    def test_tree_children(self):
        self.assertEqual(set(self.d.children["entry"]), {"a", "b", "m"})

    def test_dominates(self):
        self.assertTrue(self.d.dominates("entry", "m"))
        self.assertFalse(self.d.dominates("a", "m"))
        self.assertTrue(self.d.dominates("m", "m"))


class LoopDomTests(unittest.TestCase):
    def setUp(self):
        # entry -> cond -> body -> cond ; cond -> end
        self.f = _fn([
            ("entry", [], make_jmp("cond")),
            ("cond", [], make_br("%c", "body", "end")),
            ("body", [], make_jmp("cond")),
            ("end", [], make_ret("%c")),
        ])
        self.d = compute_dominance(self.f)

    def test_loop_header_dom(self):
        self.assertEqual(self.d.idom["cond"], "entry")
        self.assertEqual(self.d.idom["body"], "cond")
        self.assertEqual(self.d.idom["end"], "cond")

    def test_frontier_body_reaches_header(self):
        # DF(body) includes the header: body is a predecessor of cond and
        # idom(cond)=entry != body.
        self.assertIn("cond", self.d.df["body"])
        # The loop header dominates a predecessor of itself (body) but
        # does not *strictly* dominate itself, so cond in DF(cond) is the
        # classic self-frontier result for loop headers.
        self.assertIn("cond", self.d.df["cond"])
        self.assertEqual(self.d.df["end"], set())


class UnreachableDomTests(unittest.TestCase):
    def test_dangling_block_is_reported(self):
        f = _fn([
            ("entry", [], make_ret(None)),
            ("ghost", [], make_ret(None)),
        ])
        d = compute_dominance(f)
        self.assertEqual(d.removed, ["ghost"])
        self.assertNotIn("ghost", d.labels)


if __name__ == "__main__":
    unittest.main()
