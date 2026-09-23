"""字节码解释器。

只执行已经通过 :mod:`byteverifier.verifier` 验证的模块，因此解释器内部
**不再做栈下溢/未初始化检查** —— 这些是验证器的责任（深度防御除外，
``pop`` 仍会在极端情况下抛 internal 错误，一旦发生即说明验证器有 bug）。

资源限制（防止变异/恶意代码造成的无限循环）：
* ``fuel`` 最大指令步数，默认 100000；
* ``max_depth`` 最大调用深度，默认 200。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import bytecode as bc
from .bytecode import (
    BUILTIN_PRINT_BOOL, BUILTIN_PRINT_INT, TAG_BOOL, TAG_INT, TAG_VOID, Module,
)
from .common import PHASE_INTERP, PHASE_RESOURCE, ToolError

_UNSET = object()


@dataclass
class Frame:
    code: bc.CodeObject
    pc: int = 0
    stack: list = field(default_factory=list)
    locals: list = field(default_factory=list)
    pc_index: dict = field(default_factory=dict)


class Interpreter:
    def __init__(
        self,
        module: Module,
        fuel: int = 100_000,
        max_depth: int = 200,
        out=None,
    ) -> None:
        self.module = module
        self.fuel = fuel
        self.max_depth = max_depth
        self.steps = 0
        self.out = out  # 可写流（None -> 丢弃内建输出）

    def call_main(self):
        main = self.module.by_name("main")
        if main is None:
            raise ToolError(PHASE_INTERP, "missing.main", "模块中没有 main 函数")
        if main.param_tags:
            raise ToolError(PHASE_INTERP, "main.params", "main 必须无参数")
        return self._invoke(main, [])

    # ---------- 调用 ----------

    @staticmethod
    def _make_frame(code: bc.CodeObject, args: list) -> Frame:
        return Frame(
            code=code,
            locals=list(args) + [_UNSET] * len(code.local_tags),
            pc_index={ins.pc: i for i, ins in enumerate(code.instructions)},
        )

    def _invoke(self, code: bc.CodeObject, args: list):
        return self._run(self._make_frame(code, args), depth=0)

    def _run(self, frame: Frame, depth: int):
        code = frame.code
        ins_list = code.instructions

        while True:
            self.steps += 1
            if self.steps > self.fuel:
                raise ToolError(
                    PHASE_RESOURCE, "fuel.exhausted",
                    f"执行超过 {self.fuel} 步仍未结束（疑似无限循环）",
                    pc=frame.pc, func_name=code.name,
                )

            if frame.pc not in frame.pc_index:
                raise ToolError(
                    PHASE_INTERP, "pc.invalid.internal",
                    f"pc={frame.pc} 不是有效指令（验证器漏检）",
                    pc=frame.pc, func_name=code.name,
                )
            cur = ins_list[frame.pc_index[frame.pc]]
            op = cur.opcode
            nxt_pc = frame.pc + 1 + bc.OPERAND_SIZE[op]

            if op == bc.OP_LOAD_CONST:
                frame.stack.append(code.consts[cur.operand][1])

            elif op == bc.OP_LOAD_LOCAL:
                v = frame.locals[cur.operand]
                if v is _UNSET:
                    raise ToolError(  # 验证器应已排除
                        PHASE_INTERP, "local.uninit.internal",
                        "读取未初始化局部量（验证器漏检）",
                        pc=frame.pc, func_name=code.name,
                    )
                frame.stack.append(v)

            elif op == bc.OP_STORE_LOCAL:
                frame.locals[cur.operand] = frame.stack.pop()

            elif op == bc.OP_POP:
                frame.stack.pop()

            elif op == bc.OP_ADD:
                b = frame.stack.pop(); frame.stack.append(frame.stack.pop() + b)
            elif op == bc.OP_SUB:
                b = frame.stack.pop(); frame.stack.append(frame.stack.pop() - b)
            elif op == bc.OP_MUL:
                b = frame.stack.pop(); frame.stack.append(frame.stack.pop() * b)
            elif op == bc.OP_DIV:
                b = frame.stack.pop()
                if b == 0:
                    raise ToolError(
                        PHASE_INTERP, "div.zero", "整数除零",
                        pc=frame.pc, func_name=code.name,
                    )
                frame.stack.append(frame.stack.pop() // b)
            elif op == bc.OP_NEG:
                frame.stack.append(-frame.stack.pop())
            elif op == bc.OP_NOT:
                frame.stack.append(not frame.stack.pop())

            elif op == bc.OP_EQ:
                b = frame.stack.pop(); frame.stack.append(frame.stack.pop() == b)
            elif op == bc.OP_NE:
                b = frame.stack.pop(); frame.stack.append(frame.stack.pop() != b)
            elif op == bc.OP_LT:
                b = frame.stack.pop(); frame.stack.append(frame.stack.pop() < b)
            elif op == bc.OP_GT:
                b = frame.stack.pop(); frame.stack.append(frame.stack.pop() > b)
            elif op == bc.OP_LE:
                b = frame.stack.pop(); frame.stack.append(frame.stack.pop() <= b)
            elif op == bc.OP_GE:
                b = frame.stack.pop(); frame.stack.append(frame.stack.pop() >= b)

            elif op == bc.OP_JMP:
                frame.pc = cur.operand
                continue
            elif op == bc.OP_JIF:
                cond = frame.stack.pop()
                frame.pc = cur.operand if not cond else nxt_pc
                continue

            elif op == bc.OP_CALL:
                r = self._do_call(cur.operand, frame.stack, depth)
                if r is not _VOID_RESULT:
                    frame.stack.append(r)

            elif op == bc.OP_RETV:
                return frame.stack.pop()
            elif op == bc.OP_RET:
                return None

            else:  # pragma: no cover
                raise ToolError(
                    PHASE_INTERP, "opcode.internal",
                    f"未知操作码 0x{op:02x}（验证器漏检）", pc=frame.pc,
                )

            frame.pc = nxt_pc

    def _do_call(self, idx: int, stack: list, depth: int):
        if depth + 1 > self.max_depth:
            raise ToolError(
                PHASE_RESOURCE, "depth.exceeded",
                f"调用深度超过 {self.max_depth}（疑似无限递归）",
            )
        if idx == BUILTIN_PRINT_INT:
            v = stack.pop()
            if self.out is not None:
                self.out.write(str(v) + "\n")
            return _VOID_RESULT
        if idx == BUILTIN_PRINT_BOOL:
            v = stack.pop()
            if self.out is not None:
                self.out.write(("true" if v else "false") + "\n")
            return _VOID_RESULT
        if 0 <= idx < len(self.module.functions):
            callee = self.module.functions[idx]
            argc = len(callee.param_tags)
            args = stack[-argc:] if argc else []
            if argc:
                del stack[-argc:]
            return self._run(self._make_frame(callee, args),
                             depth=depth + 1)
        raise ToolError(
            PHASE_INTERP, "call.oob.internal",
            f"调用编号 {idx} 无效（验证器漏检）",
        )


_VOID_RESULT = object()


def run(module: Module, fuel: int = 100_000, out=None):
    """便捷入口：运行 main，返回 (返回值, 执行步数)。"""
    interp = Interpreter(module, fuel=fuel, out=out)
    result = interp.call_main()
    return result, interp.steps
