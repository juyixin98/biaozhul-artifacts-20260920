"""生成一个“手工构造的越界跳转”模块 oob.bv（不经编译器）。

用于演示：字节码第 2 条指令 JMP 的目标偏移被写成 0xFFFF，远超代码段，
验证器必须以 jump.oob 拒绝，并给出最短错误路径。

运行:  python examples/make_oob_module.py
随后:  python -m byteverifier verify examples/oob.bv
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from byteverifier.bytecode import (
    TAG_INT, TAG_VOID, CodeObject, Instruction, Module,
)

# LOAD_CONST 0 ; 压入常量 1（pc=0）
# JMP 65535   ; 越界跳转   （pc=3）
# POP         ;            （pc=6）
# RET         ;            （pc=7）
ins = [
    Instruction(0x10, 0, pc=0),
    Instruction(0x40, 0xFFFF, pc=3),
    Instruction(0x13, 0, pc=6),
    Instruction(0x52, 0, pc=7),
]
code = CodeObject(
    name="main", ret_tag=TAG_VOID, param_tags=[], local_tags=[],
    instructions=ins, consts=[(TAG_INT, 1)], max_stack=1,
)
# 手工模块走完整编码，再故意把声明 codelen 改到足够大以容纳目标？
# 不——保持 codelen 与自然布局一致(10/11 字节)，跳转目标本身越界即可。
blob = Module([code]).encode()
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "oob.bv")
with open(out, "wb") as f:
    f.write(blob)
print(f"已写出 {out} ({len(blob)} 字节)，其中 JMP 目标=0xFFFF")
