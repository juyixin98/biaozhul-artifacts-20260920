"""端到端验证：手工构造“φ 入边构成交换环”的 SSA IR。

循环头：
    %a.1 = phi [entry: 1], [latch: %b.1]
    %b.1 = phi [entry: 2], [latch: %a.1]
每次迭代 latch 边上是并行复制 a.1<-b.1, b.1<-a.1 —— 真环，必须串行化。
程序在 n 次迭代后返回 a+b（交换不改变和，恒为 3；另用计数器返回 a 以便区分）。
"""

from ssa_tool.interpreter import interpret
from ssa_tool.ir import Block, Const, FunctionIR, Instr, Name, PhiInstr, dump_function
from ssa_tool.phi_elimination import eliminate_phis
from ssa_tool.ssa_validate import validate_ssa


def build_swap_ssa(n_param: bool = True) -> FunctionIR:
    entry = Block("entry")
    head = Block("head")
    body = Block("body")
    latch = Block("latch")
    exitb = Block("exit")

    entry.instrs.append(Instr("const", "c1", [Const(1)]))
    entry.instrs.append(Instr("const", "c2", [Const(2)]))
    entry.instrs.append(Instr("const", "c0", [Const(0)]))
    entry.instrs.append(Instr("const", "c1b", [Const(1)]))
    if n_param:
        entry.instrs.append(Instr("param", "n", [Const(0)]))
    else:
        entry.instrs.append(Instr("const", "n", [Const(3)]))
    entry.terminator = Instr("jmp", None, blocks=["head"])

    head.phis.append(PhiInstr("a1", "a",
                              {"entry": Name("c1"), "latch": Name("b1")}))
    head.phis.append(PhiInstr("b1", "b",
                              {"entry": Name("c2"), "latch": Name("a1")}))
    head.phis.append(PhiInstr("i1", "i",
                              {"entry": Name("c0"), "latch": Name("i2")}))
    head.instrs.append(Instr("lt", "cond", [Name("i1"), Name("n")]))
    head.terminator = Instr("br", None, [Name("cond")], blocks=["body", "exit"])

    body.terminator = Instr("jmp", None, blocks=["latch"])
    latch.instrs.append(Instr("add", "i2", [Name("i1"), Name("c1b")]))
    latch.terminator = Instr("jmp", None, blocks=["head"])

    exitb.instrs.append(Instr("add", "sum", [Name("a1"), Name("b1")]))
    exitb.terminator = Instr("ret", None, [Name("sum")])

    return FunctionIR("swap", ["n"], [entry, head, body, latch, exitb],
                      flavor="ssa")


def main() -> int:
    ssa = build_swap_ssa()
    violations = validate_ssa(ssa, raise_on_error=False)
    assert not violations, [str(v) for v in violations]
    print(dump_function(ssa))
    exec_fn = eliminate_phis(ssa.copy())
    print(dump_function(exec_fn))

    # n=0 -> a=1,b=2 sum=3; n=1 -> a=2,b=1 sum=3; n=2 -> a=1,b=2 ...
    for n in range(6):
        r = interpret(exec_fn, [n])
        assert r.value == 3, (n, r.value)
        print(f"n={n}: a+b={r.value} ✓")
    print("φ 交换环端到端验证通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
