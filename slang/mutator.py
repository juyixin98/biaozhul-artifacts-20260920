"""单字节变异器（验收工具）。

策略
====
- ``targeted``（默认）：对指定函数代码区的每个字节位置，枚举出“最能
  击中验证器关键检查”的少量目标值——把该位置改成关键操作码，或把
  跳转/立即数的字节改为 0x00 / 0xFF / 0x7F / 0x80（距离边界）。
- ``exhaustive``：对代码区每个位置枚举全部 255 个异于原值的字节
  （代码较小时才实际可行，由调用方用预算控制）。

每条变异只改 code 区 1 个字节；模块“信封”（含源码调试映射）原样
重新编码，因此报错仍能回显源码位置。变异模块不做缓存写回，
调用方得到 bytes 后交给 verifier/interpreter。
"""

from __future__ import annotations

from dataclasses import dataclass

from .bytecode import (
    Module, OP_ADD, OP_JIF, OP_JUMP, OP_LOAD, OP_NOP, OP_POP, OP_PUSH,
    OP_RETV, OP_RET, OP_STORE,
)

# 变异时偏好替换成的操作码：分别能诱发
# 下溢 / 跳转 / 读槽 / 返回约定 / 立即数宽度错位等
INTERESTING_OPS = bytes([
    OP_PUSH, OP_POP, OP_LOAD, OP_STORE,
    OP_ADD, OP_JUMP, OP_JIF, OP_RET, OP_RETV, OP_NOP,
])
# 对操作数字节偏好的极值
INTERESTING_IMM = bytes([0x00, 0x01, 0x7F, 0x80, 0xFE, 0xFF])


@dataclass(frozen=True)
class Mutation:
    func_index: int
    offset: int            # 相对该函数 code 区
    orig: int
    value: int             # 变异后字节
    kind: str              # opcode | operand-byte

    @property
    def label(self) -> str:
        return f"f{self.func_index}@0x{self.offset:02x}:0x{self.orig:02x}->0x{self.value:02x}"


def _instruction_boundaries(fn) -> tuple[set[int], dict[int, int]]:
    """返回 (操作码位置集合, 操作码位置 -> 操作数宽度)。"""
    instrs = fn.decode()
    return ({i.pc for i in instrs},
            {i.pc: i.length - 1 for i in instrs})


def targeted_mutations(module: Module,
                       func_index: int | None = None) -> list[Mutation]:
    """生成有针对性的变异集合（位置去重）。"""
    targets = [func_index] if func_index is not None else list(range(len(module.funcs)))
    out: list[Mutation] = []
    for fi in targets:
        fn = module.funcs[int(fi)]
        code = bytes(fn.code)
        opcodes, widths = _instruction_boundaries(fn)
        for off, orig in enumerate(code):
            values: set[int] = set()
            if off in opcodes:
                # 操作码位置：换成其它“有意思”的操作码
                for op in INTERESTING_OPS:
                    if op != orig:
                        values.add(op)
            else:
                # 操作数位置：极值字节（覆盖跳转/立即数越界、槽号越界）
                for b in INTERESTING_IMM:
                    if b != orig:
                        values.add(b)
            for v in sorted(values):
                out.append(Mutation(
                    func_index=int(fi), offset=off, orig=orig, value=v,
                    kind="opcode" if off in opcodes else "operand-byte",
                ))
    return out


def exhaustive_mutations(module: Module,
                         func_index: int | None = None,
                         cap: int = 200_000) -> list[Mutation]:
    """代码区每位置枚举全部 255 个其它字节；超过 cap 抛错以免失控。"""
    targets = [func_index] if func_index is not None else list(range(len(module.funcs)))
    total = sum(len(module.funcs[fi].code) for fi in targets) * 255
    if total > cap:
        raise ValueError(
            f"全量变异规模 {total} 超过预算 {cap}；请改用 targeted 或缩小程序")
    out: list[Mutation] = []
    for fi in targets:
        fn = module.funcs[fi]
        opcodes, _ = _instruction_boundaries(fn)
        for off, orig in enumerate(bytes(fn.code)):
            for v in range(256):
                if v == orig:
                    continue
                out.append(Mutation(
                    func_index=fi, offset=off, orig=orig, value=v,
                    kind="opcode" if off in opcodes else "operand-byte",
                ))
    return out


def apply_mutation(module: Module, m: Mutation) -> bytes:
    """返回变异后的完整模块二进制（不修改原 module 对象）。"""
    fn = module.funcs[m.func_index]
    if not 0 <= m.offset < len(fn.code):
        raise IndexError("变异偏移越界")
    new_code = bytearray(fn.code)
    new_code[m.offset] = m.value
    return module.replace_code(m.func_index, new_code)
