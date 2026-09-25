"""支配关系：手工核对菱形、循环与不可达块的 idom / DF。"""

import unittest

from ssa_tool.dominance import (analyze, dominance_frontiers,
                                dominator_tree)
from ssa_tool.ir import Block, Const, FunctionIR, Instr, Name


def fn_from_edges(name_preds: list[tuple[str, list[str]]],
                  entry: str = "entry") -> FunctionIR:
    blocks = []
    for name, succs in name_preds:
        b = Block(name)
        if len(succs) == 1:
            b.terminator = Instr("jmp", None, blocks=list(succs))
        elif len(succs) == 2:
            b.instrs.append(Instr("const", f"c_{name}", [Const(0)]))
            b.terminator = Instr("br", None, [Name(f"c_{name}")],
                                 blocks=list(succs))
        else:
            b.terminator = Instr("ret", None, [Const(0)])
        blocks.append(b)
    return FunctionIR("f", [], blocks, "raw")


class TestDominance(unittest.TestCase):
    def test_diamond(self):
        # entry -> t,f -> merge
        fn = fn_from_edges([
            ("entry", ["t", "f"]),
            ("t", ["merge"]),
            ("f", ["merge"]),
            ("merge", []),
        ])
        dom = analyze(fn)
        self.assertEqual(dom.idom["t"], "entry")
        self.assertEqual(dom.idom["f"], "entry")
        self.assertEqual(dom.idom["merge"], "entry")
        df = dominance_frontiers(dom.order, dom.idom, dom.preds)
        self.assertEqual(df["t"], {"merge"})
        self.assertEqual(df["f"], {"merge"})
        self.assertEqual(df["entry"], set())
        self.assertTrue(dom.dominates("entry", "merge"))
        self.assertFalse(dom.dominates("t", "merge"))

    def test_loop(self):
        # entry -> head; head -> body, exit; body -> head; exit -> ret
        fn = fn_from_edges([
            ("entry", ["head"]),
            ("head", ["body", "exit"]),
            ("body", ["head"]),
            ("exit", []),
        ])
        dom = analyze(fn)
        self.assertEqual(dom.idom["head"], "entry")
        self.assertEqual(dom.idom["body"], "head")
        self.assertEqual(dom.idom["exit"], "head")
        df = dominance_frontiers(dom.order, dom.idom, dom.preds)
        self.assertEqual(df["body"], {"head"})     # 循环回边
        self.assertEqual(df["head"], {"head"})
        tree = dominator_tree(dom.idom)
        self.assertEqual(set(tree["head"]), {"body", "exit"})

    def test_unreachable_excluded(self):
        # entry -> a -> exit；dead -> a（dead 无入口可达）
        fn = fn_from_edges([
            ("entry", ["a"]),
            ("a", ["exit"]),
            ("dead", ["a"]),
            ("exit", []),
        ])
        dom = analyze(fn)
        self.assertNotIn("dead", dom.reachable)
        self.assertNotIn("dead", dom.idom)
        # exit 的前驱表只含可达块
        self.assertEqual(dom.preds["exit"], ["a"])


if __name__ == "__main__":
    unittest.main()
