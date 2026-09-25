"""手工构造的 φ 交换环：并入 unittest。"""

import importlib.util
import os
import unittest

from ssa_tool.interpreter import interpret
from ssa_tool.ir import Block, Const, FunctionIR, Instr, Name, PhiInstr
from ssa_tool.phi_elimination import eliminate_phis
from ssa_tool.ssa_validate import validate_ssa

_spec = importlib.util.spec_from_file_location(
    "manual_phi_swap",
    os.path.join(os.path.dirname(__file__), "..", "examples", "manual_phi_swap.py"))
manual = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(manual)


class TestManualPhiSwap(unittest.TestCase):
    def test_swap_cycle(self):
        ssa = manual.build_swap_ssa()
        self.assertEqual(validate_ssa(ssa, raise_on_error=False), [])
        ex = eliminate_phis(ssa.copy())
        # latch 边必须用临时量打破环
        latch = ex.block("latch")
        temps = [i.dest for i in latch.instrs
                 if i.dest and i.dest.startswith("pcopy.")]
        self.assertTrue(temps)
        ops = [i.op for i in latch.instrs]
        self.assertIn("copy", ops)
        for n in range(6):
            self.assertEqual(interpret(ex, [n]).value, 3)

    def test_three_cycle_phi(self):
        # 手工三环：a<-b, b<-c, c<-a 的 φ 回边
        entry, head, latch, exitb = Block("entry"), Block("head"), Block("latch"), Block("exit")
        entry.instrs += [Instr("const", "c1", [Const(1)]),
                         Instr("const", "c2", [Const(2)]),
                         Instr("const", "c3", [Const(3)]),
                         Instr("const", "c0", [Const(0)]),
                         Instr("const", "one", [Const(1)]),
                         Instr("param", "n", [Const(0)])]
        entry.terminator = Instr("jmp", None, blocks=["head"])
        head.phis = [
            PhiInstr("a1", "a", {"entry": Name("c1"), "latch": Name("b1")}),
            PhiInstr("b1", "b", {"entry": Name("c2"), "latch": Name("c1v")}),
            PhiInstr("c1v", "c", {"entry": Name("c3"), "latch": Name("a1")}),
            PhiInstr("i1", "i", {"entry": Name("c0"), "latch": Name("i2")}),
        ]
        head.instrs.append(Instr("lt", "cond", [Name("i1"), Name("n")]))
        head.terminator = Instr("br", None, [Name("cond")], blocks=["latch", "exit"])
        latch.instrs.append(Instr("add", "i2", [Name("i1"), Name("one")]))
        latch.terminator = Instr("jmp", None, blocks=["head"])
        exitb.instrs.append(
            Instr("add", "s1", [Name("a1"), Name("b1")]))
        exitb.instrs.append(
            Instr("add", "s2", [Name("s1"), Name("c1v")]))
        exitb.terminator = Instr("ret", None, [Name("s2")])
        ssa = FunctionIR("tri", ["n"], [entry, head, latch, exitb], "ssa")
        self.assertEqual(validate_ssa(ssa, raise_on_error=False), [])
        ex = eliminate_phis(ssa.copy())
        # 三环旋转但和恒为 6
        for n in range(5):
            self.assertEqual(interpret(ex, [n]).value, 6)


if __name__ == "__main__":
    unittest.main()
