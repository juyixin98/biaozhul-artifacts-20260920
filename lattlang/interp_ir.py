"""Reference interpreter for (optimized or unoptimized) SSA IR.

Phi semantics: when control transfers from block P to S, every phi in S
takes the argument slot corresponding to predecessor P.  We evaluate phis
once on block entry (SCCP/optimization ensures the IR is in valid SSA).

Runtime errors carry the originating instruction's source :class:`Span`,
so optimized and unoptimized programs report the same user-visible error
location for the same failing operation.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import LangError
from .ir import Imm, IRProgram
from .semantics import ZeroDivisionTrapped, apply_binary, apply_unary
from .interp_ast import MAX_STEPS, RunResult


def run_ir(prog: IRProgram, max_steps: int = MAX_STEPS) -> RunResult:
    env: dict[str, int] = {}
    output: list[str] = []
    steps = 0

    def budget() -> None:
        nonlocal steps
        steps += 1
        if steps > max_steps:
            raise LangError("step budget exceeded (possible infinite loop)",
                            "runtime")

    def value_of(op) -> int:
        return op.value if isinstance(op, Imm) else env[op]

    try:
        label = prog.entry
        # Phi argument selection needs to know the previous block; the entry
        # block never has phis with external predecessors in our construction.
        pred: str | None = None
        while True:
            budget()
            block = prog.block(label)
            if block.phis:
                idx = block.preds.index(pred) if pred in block.preds else 0
                for dest, args in block.phis.items():
                    env[dest] = value_of(args[idx])
            for ins in block.instrs:
                budget()
                if ins.kind == "const":
                    env[ins.dest] = value_of(ins.args[0])
                elif ins.kind == "copy":
                    env[ins.dest] = value_of(ins.args[0])
                elif ins.kind == "unary":
                    env[ins.dest] = apply_unary(ins.op,
                                                value_of(ins.args[0]))
                elif ins.kind == "binary":
                    a = value_of(ins.args[0])
                    b = value_of(ins.args[1])
                    try:
                        env[ins.dest] = apply_binary(ins.op, a, b)
                    except ZeroDivisionTrapped:
                        raise LangError("division or modulo by zero",
                                        "runtime", ins.span) from None
                elif ins.kind == "print":
                    output.append(str(value_of(ins.args[0])))
            t = block.term
            if t.kind == "ret":
                break
            if t.kind == "unreachable":
                span = t.span
                raise LangError("execution reached unreachable code",
                                "runtime", span if not span.synthetic else None)
            if t.kind == "jmp":
                pred, label = label, t.targets[0]
                continue
            # br
            cond = value_of(t.cond)
            target = t.targets[0] if cond != 0 else t.targets[1]
            pred, label = label, target
    except LangError as e:
        return RunResult(output, steps, e)
    return RunResult(output, steps)
