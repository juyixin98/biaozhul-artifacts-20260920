"""手工汇编“故意非法”的字节码模块，用于演示验证器各类错误。

运行: python examples/make_bad_modules.py
在 examples/bad/ 下生成若干 hex 模块：

  bad_oob_jump.bin   跳转目标越过函数末尾          -> JUMP_OUT_OF_BOUNDS
  bad_backedge.bin   回边目标落在操作数中间        -> JUMP_UNALIGNED
  bad_stack_merge.bin 分支两路栈高度不一致后合流   -> STACK_MERGE_CONFLICT
  bad_type_merge.bin  两路分别压 int/bool 后合流读取 -> TYPE_MISMATCH
  bad_uninit.bin      if 一路给局部量赋值，合流后读取 -> LOCAL_UNINITIALIZED
  bad_underflow.bin   空栈直接 ADD                 -> STACK_UNDERFLOW
  bad_retv.bin        void 函数 RETV               -> RETURN_MISMATCH

这些模块不经过编译器，直接按 docs/BYTECODE.md 的二进制格式拼装，
证明验证器并不依赖“编译器是善意的”。
"""

from __future__ import annotations

import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from slang.bytecode import (  # noqa: E402
    MAGIC, T_INT, T_BOOL, T_VOID,
    OP_ADD, OP_JIF, OP_JUMP, OP_LOAD, OP_NOP, OP_POP, OP_PUSH,
    OP_RET, OP_RETV, OP_STORE, OP_TRUE,
    FuncCode, Module,
)

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "bad")


def u16(n: int) -> bytes:
    return struct.pack(">H", n)


def s16(n: int) -> bytes:
    return struct.pack(">h", n)


def build(funcs: list[FuncCode], source: str = "; hand-crafted\n") -> bytes:
    out = bytearray(MAGIC)
    out += u16(len("bad")) + b"bad"
    sb = source.encode()
    out += u16(len(sb)) + sb
    out += u16(len(funcs))
    for f in funcs:
        nb = f.name.encode()
        out.append(len(nb))
        out += nb
        out.append(len(f.param_types))
        out += bytes(f.param_types)
        out.append(f.ret_type)
        out += u16(len(f.local_types))
        out += bytes(f.local_types)
        out += struct.pack(">I", len(f.code))
        out += f.code
        out += u16(0)   # 无调试映射
    return bytes(out)


def main() -> None:
    os.makedirs(OUT, exist_ok=True)

    # 1) 越界跳转：JUMP 相对偏移 +100，远超代码长度
    code = bytes([OP_JUMP]) + s16(100)
    open(os.path.join(OUT, "bad_oob_jump.bin"), "w").write(
        build([FuncCode("f", [], T_VOID, [], bytearray(code))]).hex())

    # 2) 回边未对齐：PUSH(3字节) 位于开头，回边跳到 pc=1（操作数中间）
    code = bytes([OP_PUSH]) + s16(1) + bytes([OP_NOP, OP_JUMP]) + s16(1 - 4)
    # pc0=PUSH(0..2), pc3=NOP, pc4=JUMP(4..6), rel = 1-4 = -3 -> 目标 pc=1
    open(os.path.join(OUT, "bad_backedge.bin"), "w").write(
        build([FuncCode("f", [], T_VOID, [T_INT], bytearray(code))]).hex())

    # 3) 栈高度合流失败：
    #    条件真分支在合流前栈上留 2 个 int，假分支只留 1 个 int -> 高度冲突。
    #      0: TRUE                 条件
    #      1: JIF -> L_taken       真走 taken
    #      4: PUSH 8               假分支：留 1 个
    #      7: JUMP -> end
    #    L_taken(10):
    #     10: PUSH 8               真分支：留 2 个
    #     13: PUSH 9
    #    end(16): RETV(int 函数，但两路栈高不同，先报 STACK_MERGE_CONFLICT)
    code = bytearray()
    code += bytes([OP_TRUE, OP_JIF, 0, 0])          # 0..3
    code += bytes([OP_PUSH]) + s16(8)               # 4..6
    code += bytes([OP_JUMP, 0, 0])                  # 7..9
    l_taken = len(code)                             # 10
    code += bytes([OP_PUSH]) + s16(8)               # 10..12
    code += bytes([OP_PUSH]) + s16(9)               # 13..15
    end = len(code)                                 # 16
    code += bytes([OP_RETV])                        # 16
    struct.pack_into(">h", code, 2, l_taken - 1)
    struct.pack_into(">h", code, 8, end - 7)
    open(os.path.join(OUT, "bad_stack_merge.bin"), "w").write(
        build([FuncCode("f", [], T_INT, [], bytes(code))]).hex())

    # 4) 类型合流失败：两路分别压 int / bool，合流后 RETV（声明 int）
    #    TRUE; JIF L1; JUMP L2; L1: PUSH 5; JUMP E; L2: TRUE; E: RETV
    #    简化：TRUE; JIF L; JUMP after; L: PUSH 5; after: RETV
    code = bytearray()
    code += bytes([OP_TRUE, OP_JIF]) + s16(0)      # pc0,1..2 占位
    jif_op_pc = 1
    # false 路径顺序：压 bool（已经有 TRUE 在栈上？JIF 会弹掉条件）
    # 设计：条件单独再压一个 TRUE 作为跳转条件
    code = bytearray([OP_TRUE, OP_JIF, 0, 0])     # 条件
    # true -> L_int: PUSH 5; JUMP end
    # false(fallthrough): TRUE
    code += bytes([OP_JUMP, 0, 0])                # 跳过 false 分支? 反转
    # 重新清晰构造（见下）
    code = bytearray()
    code += bytes([OP_TRUE])                      # 0 条件
    code += bytes([OP_JIF, 0, 0])                 # 1 -> L_int（回填）
    # false 分支（顺序）：压 bool
    code += bytes([OP_TRUE])                      # 4
    code += bytes([OP_JUMP, 0, 0])                # 5 -> end（回填）
    l_int = len(code)
    code += bytes([OP_PUSH]) + s16(5)             # true 分支压 int
    j_end_after_true = len(code)
    end = j_end_after_true + 0                    # 先记录，end 在追加后
    code += bytes([OP_JUMP, 0, 0])                # -> end
    end = len(code)
    code += bytes([OP_RETV])
    struct.pack_into(">h", code, 2, l_int - 1)    # JIF @1
    struct.pack_into(">h", code, 6, end - 5)      # JUMP @5(false 分支尾)
    struct.pack_into(">h", code, j_end_after_true + 1, end - j_end_after_true)
    open(os.path.join(OUT, "bad_type_merge.bin"), "w").write(
        build([FuncCode("f", [], T_INT, [], bytes(code))]).hex())

    # 5) 局部量未初始化：1 个 int 槽；不写；直接 LOAD 0；POP；RET
    code = bytes([OP_LOAD, 0, OP_POP, OP_RET])
    open(os.path.join(OUT, "bad_uninit.bin"), "w").write(
        build([FuncCode("f", [], T_VOID, [T_INT], bytearray(code))]).hex())

    # 6) 空栈 ADD：ADD; RET
    code = bytes([OP_ADD, OP_RET])
    open(os.path.join(OUT, "bad_underflow.bin"), "w").write(
        build([FuncCode("f", [], T_VOID, [], bytearray(code))]).hex())

    # 7) void 函数用 RETV：PUSH 1; RETV
    code = bytes([OP_PUSH]) + s16(1) + bytes([OP_RETV])
    open(os.path.join(OUT, "bad_retv.bin"), "w").write(
        build([FuncCode("f", [], T_VOID, [], bytearray(code))]).hex())

    print(f"已在 {OUT} 生成 7 个非法模块")


if __name__ == "__main__":
    main()
