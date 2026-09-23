"""变异活动：穷举小模块的所有单 bit 翻转 + 定向越界/回边/异常返回。

用法::

    python scripts/mutation_campaign.py [src.vl]

输出每种结果的计数，并断言“解释器不发生栈下溢/内部错误”这一健全性不变式。
"""

from __future__ import annotations

import io
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from byteverifier import mutate as mut
from byteverifier.bytecode import JUMP_OPS, decode_module
from byteverifier.common import ToolError
from byteverifier.compiler import compile_source
from byteverifier.interpreter import Interpreter
from byteverifier.verifier import verify_module

DEFAULT_SRC = """
int sumto(int n) {
    int s = 0;
    int i = 1;
    while (i <= n) { s = s + i; i = i + 1; }
    return s;
}
void main() {
    print_int(sumto(4));
    if (sumto(1) == 1) { print_bool(true); } else { print_bool(false); }
}
"""

UNSOUND = ("runtime:pc.invalid", "runtime:stack", "IndexError",
           "runtime:local.uninit.internal", "runtime:opcode.internal",
           "runtime:call.oob.internal")


def classify(mutated: bytes) -> str:
    try:
        module = decode_module(mutated)
    except ToolError as e:
        return f"decode:{e.kind}"
    try:
        verify_module(module)
    except ToolError as e:
        return f"verify:{e.kind}"
    if module.by_name("main") is None:
        return "ran:no-main"
    try:
        Interpreter(module, fuel=5_000, out=io.StringIO()).call_main()
        return "ran:ok"
    except ToolError as e:
        return f"runtime:{e.kind}"


def main() -> int:
    if len(sys.argv) > 1:
        with open(sys.argv[1], encoding="utf-8") as f:
            src = f.read()
    else:
        src = DEFAULT_SRC

    module = compile_source(src)
    blob = module.encode()
    print(f"模块大小: {len(blob)} 字节，函数: "
          f"{[f.name for f in module.functions]}")

    counter: Counter[str] = Counter()
    unsound_cases = []
    total = len(blob) * 8
    for i, bit, mutated in mut.all_single_bit_flips(blob):
        r = classify(mutated)
        counter[r] += 1
        if any(m in r for m in UNSOUND):
            unsound_cases.append((i, bit, r))

    print(f"\n=== 单 bit 翻转穷举（共 {total} 个变异体）===")
    for kind, n in sorted(counter.items()):
        print(f"  {n:5d}  {kind}")

    # 定向：越界跳转
    regions = {name: (s, e)
               for name, s, e in mut.function_regions(blob)}
    targeted = Counter()
    for f in module.functions:
        cstart, cend = regions[f.name]
        clen = cend - cstart
        for ins in f.instructions:
            if ins.opcode not in JUMP_OPS:
                continue
            for which, addr in (("lo", cstart + ins.pc + 1),
                                ("hi", cstart + ins.pc + 2)):
                for val in (0x00, 0xFF):
                    m2 = mut.replace_byte(blob, addr, val)
                    targeted[classify(m2)] += 1
    print("\n=== 定向跳转立即数替换（每跳转 x 高低字节 x 0x00/0xFF）===")
    for kind, n in sorted(targeted.items()):
        print(f"  {n:5d}  {kind}")

    # 定向：异常返回 RETV->RET / RET->RETV
    ret_counter: Counter[str] = Counter()
    for f in module.functions:
        cstart, _ = regions[f.name]
        for ins in f.instructions:
            if ins.opcode in (0x51, 0x52):
                newop = 0x52 if ins.opcode == 0x51 else 0x51
                m2 = mut.replace_byte(blob, cstart + ins.pc, newop)
                ret_counter[classify(m2)] += 1
    print("\n=== 定向返回指令替换 RETV<->RET ===")
    for kind, n in sorted(ret_counter.items()):
        print(f"  {n:5d}  {kind}")

    ok = not unsound_cases
    print("\n健全性（解释器不发生栈下溢/内部错误）:",
          "通过 ✅" if ok else f"失败 ❌ {unsound_cases[:5]}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
