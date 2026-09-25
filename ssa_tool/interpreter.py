"""IR 解释器：可执行 raw / ssa / exec 三种形态的同一份 :class:`FunctionIR`。

用于验收对照：原始 IR、SSA IR、φ 消除后的可执行 IR，对相同输入必须给出
相同的返回值与执行步数。

语义：

- 整数为任意精度 Python 整数；``/``、``%`` 截断向零（C/Java 风格）；
- 比较与逻辑运算产生 0/1；
- raw 形态用内存槽（dict 模拟），ssa/exec 形态所有值都在寄存器表里；
- φ 在进入目标块时、执行该块任何普通指令前读取。
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import RuntimeExecError
from .ir import Const, FunctionIR, Name, Operand


def trunc_div(a: int, b: int) -> int:
    q = abs(a) // abs(b)
    return q if (a < 0) == (b < 0) else -q


def trunc_mod(a: int, b: int) -> int:
    return a - trunc_div(a, b) * b


@dataclass
class ExecResult:
    value: int
    steps: int
    trace: list[str]


class Interpreter:
    def __init__(self, fn: FunctionIR, args: list[int] | None = None,
                 max_steps: int = 1_000_000, trace: bool = False):
        self.fn = fn
        self.args = args or []
        self.max_steps = max_steps
        self.regs: dict[str, int] = {}
        self.mem: dict[str, int] = {}
        self.steps = 0
        self.trace: list[str] = []
        self.do_trace = trace
        self._by_name = {b.name: b for b in fn.blocks}

    # ---------------- 基础 ----------------

    def _read(self, op: Operand) -> int:
        if isinstance(op, Const):
            return op.value
        if op.name not in self.regs:
            raise RuntimeExecError(f"读取未定义寄存器 %{op.name}")
        return self.regs[op.name]

    def _tick(self) -> None:
        self.steps += 1
        if self.steps > self.max_steps:
            raise RuntimeExecError(f"执行步数超过上限 {self.max_steps}（疑似死循环）")

    def _err(self, ins, msg: str) -> RuntimeExecError:
        span = getattr(ins, "span", None)
        return RuntimeExecError(msg, span.start_line if span else None,
                                span.start_col if span else None)

    # ---------------- 运算 ----------------

    def _bin(self, op: str, a: int, b: int, ins) -> int:
        if op == "add": return a + b
        if op == "sub": return a - b
        if op == "mul": return a * b
        if op in ("div", "mod"):
            if b == 0:
                raise self._err(ins, "整数除以零")
            return trunc_div(a, b) if op == "div" else trunc_mod(a, b)
        if op == "lt": return int(a < b)
        if op == "le": return int(a <= b)
        if op == "gt": return int(a > b)
        if op == "ge": return int(a >= b)
        if op == "eq": return int(a == b)
        if op == "ne": return int(a != b)
        raise self._err(ins, f"未知二元运算 {op}")

    def _un(self, op: str, a: int) -> int:
        if op == "neg": return -a
        if op == "lnot": return int(a == 0)
        raise RuntimeExecError(f"未知一元运算 {op}")

    # ---------------- 主循环 ----------------

    def run(self) -> ExecResult:
        if len(self.args) < len(self.fn.params):
            raise RuntimeExecError(
                f"参数数量不足：main 需要 {len(self.fn.params)} 个，"
                f"提供 {len(self.args)} 个")

        cur = self.fn.entry.name
        self._pred_on_entry: str | None = None
        while True:
            self._tick()
            block = self._by_name[cur]
            if self.do_trace:
                self.trace.append(f"-> @{cur}")

            for phi in block.phis:
                pred = self._pred_on_entry
                if pred not in phi.incoming:
                    raise RuntimeExecError(
                        f"φ %{phi.dest} @{cur} 没有来自前驱 @{pred} 的入边")
                self.regs[phi.dest] = self._read(phi.incoming[pred])

            for ins in block.instrs:
                self._tick()
                self._exec_instr(ins)

            term = block.terminator
            if term is None:
                raise self._err(block, f"块 @{cur} 缺少终结指令")
            if term.op == "jmp":
                self._pred_on_entry = cur
                cur = term.blocks[0]
            elif term.op == "br":
                cond = self._read(term.operands[0])
                target = term.blocks[0] if cond != 0 else term.blocks[1]
                self._pred_on_entry = cur
                cur = target
            elif term.op == "ret":
                value = self._read(term.operands[0]) if term.operands else 0
                return ExecResult(value, self.steps, self.trace)
            elif term.op == "unreachable":
                raise self._err(term, "执行到达标记为 unreachable 的不可达块")
            else:
                raise self._err(term, f"未知终结指令 {term.op}")

    # ---------------- 指令 ----------------

    def _exec_instr(self, ins):
        raw_mem = self.fn.flavor == "raw"
        if ins.op == "alloc":
            self.mem[ins.dest] = self._read(ins.operands[0]) if ins.operands else 0
            return
        if ins.op == "store":
            self.mem[ins.slot] = self._read(ins.operands[0])
            return
        if ins.op == "load":
            if ins.slot not in self.mem:
                # 未经初始化即读取（raw 构建器已保证 0 初始化，这里再兜底）
                self.mem[ins.slot] = 0
            self.regs[ins.dest] = self.mem[ins.slot]
            return
        if ins.op == "const":
            self.regs[ins.dest] = self._read(ins.operands[0])
            return
        if ins.op == "param":
            idx = self._read(ins.operands[0])
            if idx >= len(self.args):
                raise self._err(ins, f"缺少第 {idx + 1} 个运行参数")
            self.regs[ins.dest] = self.args[idx]
            return
        if ins.op == "copy":
            self.regs[ins.dest] = self._read(ins.operands[0])
            return
        if ins.op in ("add", "sub", "mul", "div", "mod",
                      "lt", "le", "gt", "ge", "eq", "ne"):
            a = self._read(ins.operands[0]); b = self._read(ins.operands[1])
            self.regs[ins.dest] = self._bin(ins.op, a, b, ins)
            return
        if ins.op in ("neg", "lnot"):
            self.regs[ins.dest] = self._un(ins.op, self._read(ins.operands[0]))
            return
        raise self._err(ins, f"未知指令 {ins.op}")


def interpret(fn: FunctionIR, args: list[int] | None = None,
              max_steps: int = 1_000_000, trace: bool = False) -> ExecResult:
    return Interpreter(fn, args, max_steps, trace).run()
