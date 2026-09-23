"""验证通过后的字节码解释器。

设计要点
========
- 运行前强制先跑验证器；存在任何 VerifyError 即拒绝执行；
- 值用带标签元组表示：``("i", int)`` / ``("b", bool)``，避免 Python
  里 bool 是 int 子类造成的混淆；
- 每次弹栈都走 ``_pop``：**栈下溢会抛 InvariantBroken 而不是普通
  运行期错误**——对所有“验证通过”的模块它绝不应发生，这是验收
  要求“通过验证后解释器不应发生栈下溢”的运行期断言；
- 除零、燃料耗尽、递归过深才是合法运行期错误 RuntimeErr；
- JIF 条件、算术运算数的标签也做防御性检查（验证器应已排除错配）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .bytecode import (
    MAX_STACK,
    OP_ADD, OP_AND, OP_CALL, OP_DIV, OP_DUP, OP_EQ, OP_FALSE, OP_GE, OP_GT,
    OP_JIF, OP_JUMP, OP_LE, OP_LOAD, OP_LT, OP_MOD, OP_MUL, OP_NE,
    OP_NEG, OP_NOP, OP_NOT, OP_OR, OP_POP, OP_PRINT, OP_PUSH, OP_RETV,
    OP_RET, OP_STORE, OP_SUB, OP_TRUE,
    Module,
)
from .errors import InvariantBroken, RuntimeErr
from .verifier import verify_module

DEFAULT_FUEL = 1_000_000
DEFAULT_MAX_DEPTH = 200

I = "i"
B = "b"


def ival(n: int) -> tuple[str, int]:
    return (I, n)


def bval(x: bool) -> tuple[str, object]:
    return (B, bool(x))


@dataclass
class Frame:
    func_index: int
    pc: int = 0
    stack: list[tuple[str, object]] = field(default_factory=list)
    # 局部量：None 表示尚未赋值（验证通过的代码不会读到 None）
    locals: list[tuple[str, object] | None] = field(default_factory=list)


@dataclass
class RunResult:
    return_value: tuple[str, object] | None
    printed: list[str]
    steps: int
    max_stack: int


class Interpreter:
    def __init__(self, module: Module, fuel: int = DEFAULT_FUEL,
                 max_depth: int = DEFAULT_MAX_DEPTH,
                 verify_first: bool = True) -> None:
        self.mod = module
        self.fuel = fuel
        self.max_depth = max_depth
        self.printed: list[str] = []
        self.steps = 0
        self.max_stack_seen = 0
        self._decoded: list = []

        if verify_first:
            errs = verify_module(module)
            if errs:
                raise InvariantBroken(
                    "解释器拒绝执行未通过验证的模块；首条错误: "
                    + errs[0].message)

        for fn in module.funcs:
            self._decoded.append(fn.decode())

    # ---------- 栈原语（含不变量断言） ----------
    def _push(self, frame: Frame, v: tuple[str, object]) -> None:
        if len(frame.stack) >= MAX_STACK:
            raise InvariantBroken(f"栈高超过 {MAX_STACK}（验证器应已拦截）")
        frame.stack.append(v)
        if len(frame.stack) > self.max_stack_seen:
            self.max_stack_seen = len(frame.stack)

    def _pop(self, frame: Frame) -> tuple[str, object]:
        if not frame.stack:
            raise InvariantBroken(
                f"栈下溢 @函数 {self.mod.funcs[frame.func_index].name} "
                f"pc={frame.pc}：验证器曾判定安全，此为不变量破坏")
        return frame.stack.pop()

    def _expect(self, frame: Frame, tag: str, what: str) -> tuple[str, object]:
        v = self._pop(frame)
        if v[0] != tag:
            raise InvariantBroken(
                f"{what} 需要类型 {tag}，实际 {v[0]}（验证器应已拦截类型错配）")
        return v

    # ---------- 主入口 ----------
    def run_main(self) -> RunResult:
        mi = self.mod.func_index("main")
        if mi < 0:
            raise RuntimeErr("模块没有 main 函数")
        ret = self._call(mi, [], depth=0)
        return RunResult(ret, list(self.printed), self.steps, self.max_stack_seen)

    def call(self, name: str, args: list[tuple[str, object]]) -> RunResult:
        fi = self.mod.func_index(name)
        if fi < 0:
            raise RuntimeErr(f"没有函数 {name!r}")
        ret = self._call(fi, args, depth=0)
        return RunResult(ret, list(self.printed), self.steps, self.max_stack_seen)

    # ---------- 函数调用（递归） ----------
    def _call(self, fi: int, args: list[tuple[str, object]],
              depth: int) -> tuple[str, object] | None:
        if depth > self.max_depth:
            raise RuntimeErr(f"调用栈深度超过 {self.max_depth}（可能无限递归）")
        fn = self.mod.funcs[fi]
        instrs = self._decoded[fi]
        by_pc = {ins.pc: ins for ins in instrs}
        frame = Frame(func_index=fi)
        frame.locals = [None] * len(fn.local_types)
        for k, a in enumerate(args):
            frame.locals[k] = a

        while True:
            if self.steps >= self.fuel:
                raise RuntimeErr(
                    f"执行步数超过燃料 {self.fuel}（可能死循环）",
                    frame.pc, fn.name)
            ins = by_pc.get(frame.pc)
            if ins is None:  # 解码/边界问题，验证器应已拦截
                raise InvariantBroken(
                    f"pc={frame.pc} 不是有效指令（验证器应已拦截）")
            frame.pc = ins.pc
            self.steps += 1
            op = ins.op
            nxt = ins.pc + ins.length

            if op == OP_PUSH:
                self._push(frame, ival(ins.operand or 0))
            elif op == OP_TRUE:
                self._push(frame, bval(True))
            elif op == OP_FALSE:
                self._push(frame, bval(False))
            elif op == OP_LOAD:
                v = frame.locals[ins.operand]
                if v is None:
                    raise InvariantBroken(
                        f"读取未初始化局部槽 #{ins.operand}（验证器应已拦截）")
                self._push(frame, v)
            elif op == OP_STORE:
                v = self._pop(frame)
                frame.locals[ins.operand] = v
            elif op == OP_POP:
                self._pop(frame)
            elif op == OP_DUP:
                if not frame.stack:
                    raise InvariantBroken("DUP 栈下溢（验证器应已拦截）")
                self._push(frame, frame.stack[-1])
            elif op in (OP_ADD, OP_SUB, OP_MUL, OP_DIV, OP_MOD):
                b = self._expect(frame, I, "算术运算右操作数")
                a = self._expect(frame, I, "算术运算左操作数")
                x, y = a[1], b[1]
                if op == OP_ADD:
                    r = x + y
                elif op == OP_SUB:
                    r = x - y
                elif op == OP_MUL:
                    r = x * y
                elif op == OP_DIV:
                    if y == 0:
                        raise RuntimeErr("整数除以零", ins.pc, fn.name)
                    r = int(x / y)                     # 向零截断
                else:
                    if y == 0:
                        raise RuntimeErr("对零取模", ins.pc, fn.name)
                    r = x - int(x / y) * y             # 与向零截断除法一致
                self._push(frame, ival(int(r)))
            elif op in (OP_EQ, OP_NE, OP_LT, OP_LE, OP_GT, OP_GE):
                b = self._expect(frame, I, "")
                a = self._expect(frame, I, "")
                x, y = a[1], b[1]
                r = {OP_EQ: x == y, OP_NE: x != y, OP_LT: x < y,
                     OP_LE: x <= y, OP_GT: x > y, OP_GE: x >= y}[op]
                self._push(frame, bval(r))
            elif op == OP_AND or op == OP_OR:
                b = self._expect(frame, B, "")
                a = self._expect(frame, B, "")
                r = (a[1] and b[1]) if op == OP_AND else (a[1] or b[1])
                self._push(frame, bval(bool(r)))
            elif op == OP_NOT:
                a = self._expect(frame, B, "")
                self._push(frame, bval(not a[1]))
            elif op == OP_NEG:
                a = self._expect(frame, I, "")
                self._push(frame, ival(-a[1]))
            elif op == OP_PRINT:
                a = self._pop(frame)
                text = str(a[1]).lower() if a[0] == B else str(a[1])
                self.printed.append(text)
            elif op == OP_JUMP:
                frame.pc = ins.target()
                continue
            elif op == OP_JIF:
                cond = self._expect(frame, B, "JIF 条件")
                if cond[1]:
                    frame.pc = ins.target()
                    continue
            elif op == OP_CALL:
                callee_i = ins.operand
                callee = self.mod.funcs[callee_i]
                nargs = len(callee.param_types)
                if len(frame.stack) < nargs:
                    raise InvariantBroken("CALL 栈下溢（验证器应已拦截）")
                args_v = frame.stack[-nargs:]
                del frame.stack[-nargs:]
                ret_v = self._call(callee_i, list(args_v), depth + 1)
                if callee.ret_type != 0:
                    if ret_v is None:
                        raise InvariantBroken(
                            f"{callee.name} 声明带值返回却返回空（验证器应已拦截）")
                    self._push(frame, ret_v)
            elif op == OP_RET:
                return None
            elif op == OP_RETV:
                v = self._pop(frame)
                return v
            elif op == OP_NOP:
                pass
            else:
                raise InvariantBroken(
                    f"解释器遇到未知操作码 0x{op:02x}（验证器应已拦截）")

            frame.pc = nxt


def run(module: Module, fuel: int = DEFAULT_FUEL) -> RunResult:
    return Interpreter(module, fuel=fuel).run_main()


def format_value(v: tuple[str, object] | None) -> str:
    if v is None:
        return "void"
    return str(v[1]).lower() if v[0] == B else str(v[1])
