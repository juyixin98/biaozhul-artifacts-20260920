"""SSA 构造与校验：菱形、循环携带变量、不可达块、短路与单一定义检查。"""

import unittest

from ssa_tool.errors import SSAError
from ssa_tool.interpreter import interpret
from ssa_tool.ir_builder import build_ir
from ssa_tool.parser import parse
from ssa_tool.phi_elimination import eliminate_phis
from ssa_tool.ssa_construction import construct_ssa
from ssa_tool.ssa_validate import validate_ssa


def compile_to_ssa(src):
    raw = build_ir(parse(src))
    ssa = construct_ssa(raw.copy())
    violations = validate_ssa(ssa, raise_on_error=False)
    return raw, ssa, violations


def count_defs(ssa):
    """返回 {名字: 定义次数}。"""
    defs = {}
    for b in ssa.blocks:
        for phi in b.phis:
            defs[phi.dest] = defs.get(phi.dest, 0) + 1
        for ins in b.instrs:
            if ins.dest is not None:
                defs[ins.dest] = defs.get(ins.dest, 0) + 1
    return defs


DIAMOND = """
func main(a) {
    var x;
    if (a > 0) { x = 10; } else { x = 20; }
    return x;
}
"""

LOOP = """
func main(n) {
    var sum = 0;
    var i = 1;
    while (i <= n) {
        sum = sum + i;
        i = i + 1;
    }
    return sum;
}
"""

UNREACHABLE = """
func main(a) {
    var x = 0;
    if (a > 0) {
        x = 1;
        return x;
        x = 2;        // 不可达
    } else {
        x = 3;
    }
    return x + 1;
}
"""

SHORT_CIRCUIT = """
func main(a, b) {
    var x = 0;
    if (a > 5 && b > 5) { x = 100; }
    if (a < 0 || b < 0) { x = x + 7; }
    return x;
}
"""


class TestSSAConstruction(unittest.TestCase):
    def test_diamond_phi(self):
        _, ssa, violations = compile_to_ssa(DIAMOND)
        self.assertEqual(violations, [])
        # merge 块应有一个 x 的 φ
        merge = [b for b in ssa.blocks if b.name.startswith("merge")][0]
        self.assertEqual(len(merge.phis), 1)
        self.assertEqual(merge.phis[0].slot, "x")
        self.assertEqual(len(merge.phis[0].incoming), 2)

    def test_loop_carried_phis(self):
        _, ssa, violations = compile_to_ssa(LOOP)
        self.assertEqual(violations, [])
        head = [b for b in ssa.blocks if "head" in b.name][0]
        slots = {p.slot for p in head.phis}
        self.assertEqual(slots, {"sum", "i"})
        for phi in head.phis:
            self.assertEqual(set(phi.incoming), {"entry", "while.body.1"})

    def test_unreachable_blocks(self):
        raw, ssa, violations = compile_to_ssa(UNREACHABLE)
        self.assertEqual(violations, [])
        # 不可达块仍然保留在 SSA IR 中
        dead = [b for b in ssa.blocks if b.name.startswith("dead")]
        self.assertTrue(dead)

    def test_no_memory_instructions(self):
        _, ssa, _ = compile_to_ssa(LOOP)
        for b in ssa.blocks:
            ops = {i.op for i in b.instrs}
            self.assertFalse(ops & {"alloc", "load", "store"})

    def test_single_definition(self):
        for src in (DIAMOND, LOOP, UNREACHABLE, SHORT_CIRCUIT):
            _, ssa, _ = compile_to_ssa(src)
            defs = count_defs(ssa)
            dup = {k: v for k, v in defs.items() if v > 1}
            self.assertEqual(dup, {}, msg=f"重复定义: {dup}")

    def test_short_circuit_results(self):
        raw, ssa, _ = compile_to_ssa(SHORT_CIRCUIT)
        exec_fn = eliminate_phis(ssa.copy())
        cases = {(6, 6): 100, (6, 0): 0, (0, 6): 0, (-1, 6): 7,
                 (6, -1): 7, (-1, -1): 7}
        for args, want in cases.items():
            r1 = interpret(raw, list(args))
            r2 = interpret(ssa, list(args))
            r3 = interpret(exec_fn, list(args))
            self.assertEqual((r1.value, r2.value, r3.value),
                             (want, want, want), msg=str(args))

    def test_validator_detects_use_before_def(self):
        # 手工破坏 SSA：让加法引用一个不存在的名字
        _, ssa, _ = compile_to_ssa(DIAMOND)
        merge = [b for b in ssa.blocks if b.name.startswith("merge")][0]
        from ssa_tool.ir import Const, Name
        ret_const = merge.terminator
        # 先插入一条引用幽灵名字的指令
        from ssa_tool.ir import Instr
        merge.instrs.insert(0, Instr("add", "ghostuser",
                                     [Name("ghost"), Const(1)]))
        violations = validate_ssa(ssa, raise_on_error=False)
        self.assertTrue(any("ghost" in v.message for v in violations))

    def test_validator_detects_duplicate(self):
        _, ssa, _ = compile_to_ssa(DIAMOND)
        merge = [b for b in ssa.blocks if b.name.startswith("merge")][0]
        from ssa_tool.ir import Const, Instr
        # 再用 φ 目的名定义一次
        merge.instrs.insert(0, Instr("const", merge.phis[0].dest, [Const(0)]))
        violations = validate_ssa(ssa, raise_on_error=False)
        self.assertTrue(any(v.kind == "MULTI_DEF" for v in violations))

    def test_validator_detects_bad_phi_incoming(self):
        _, ssa, _ = compile_to_ssa(DIAMOND)
        merge = [b for b in ssa.blocks if b.name.startswith("merge")][0]
        del merge.phis[0].incoming[next(iter(merge.phis[0].incoming))]
        violations = validate_ssa(ssa, raise_on_error=False)
        self.assertTrue(any(v.kind == "PHI_INCOMING" for v in violations))


if __name__ == "__main__":
    unittest.main()
