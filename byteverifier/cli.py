"""byteverifier 命令行入口。

用法::

    python -m byteverifier compile <src.vl> [-o module.bv]
    python -m byteverifier verify <module.bv>
    python -m byteverifier run <src.vl> [--fuel N]
    python -m byteverifier disasm <src.vl|module.bv>
    python -m byteverifier mutate <module.bv> --byte N [--bit K | --value V]
    python -m byteverifier mutation-demo <src.vl>
    python -m byteverifier serve [--host H] [--port P]
"""

from __future__ import annotations

import argparse
import sys

from . import mutate as mutate_mod
from .bytecode import JUMP_OPS, OPERAND_SIZE, decode_module, disassemble
from .common import ToolError
from .compiler import compile_source
from .errorpath import render_path
from .interpreter import Interpreter
from .pipeline import compile_and_verify
from .verifier import verify_module


def _read_source(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _print_error(e: ToolError) -> None:
    """统一的人类可读错误输出（含源码片段 + 最短路径）。"""
    where = e.func_name or ""
    loc = ""
    if e.span is not None:
        loc = f"\n  位置: {e.span.short()}"
        # 尝试重新读取源码渲染片段
        try:
            import os
            if e.span.filename and os.path.exists(e.span.filename):
                lines = _read_source(e.span.filename).splitlines()
                snip = e.span.snippet(lines)
                if snip:
                    loc += "\n" + snip
        except OSError:
            pass
    pc = f" offset={e.pc}" if e.pc is not None else ""
    print(f"[{e.phase}] {e.kind}: {e.message}", file=sys.stderr)
    if where or pc:
        print(f"  函数: {where}{pc}", file=sys.stderr)
    if loc:
        print(loc, file=sys.stderr)
    if e.path is not None and getattr(e.path, "steps", None):
        print(render_path(e.path), file=sys.stderr)


def cmd_compile(args) -> int:
    source = _read_source(args.source)
    module = compile_source(source, args.source)
    blob = module.encode()
    out_path = args.output or (args.source.rsplit(".", 1)[0] + ".bv")
    with open(out_path, "wb") as f:
        f.write(blob)
    print(f"已编译 {len(module.functions)} 个函数 -> {out_path} ({len(blob)} 字节)")
    return 0


def cmd_verify(args) -> int:
    with open(args.module, "rb") as f:
        data = f.read()
    module = decode_module(data)
    verify_module(module)
    print(f"验证通过: {len(module.functions)} 个函数")
    for fn in module.functions:
        print(f"  - {fn.name}: max_stack={fn.max_stack}, 槽位={fn.nslots}")
    return 0


def cmd_run(args) -> int:
    source = _read_source(args.source)
    module = compile_and_verify(source, args.source)
    interp = Interpreter(module, fuel=args.fuel, out=sys.stdout)
    result = interp.call_main()
    print(f"[main 返回 {result!r}，{interp.steps} 步]")
    return 0


def cmd_disasm(args) -> int:
    path = args.path
    if path.endswith(".bv"):
        with open(path, "rb") as f:
            module = decode_module(f.read())
    else:
        module = compile_source(_read_source(path), path)
    for fn in module.functions:
        print(disassemble(fn))
        print()
    return 0


def cmd_mutate(args) -> int:
    with open(args.module, "rb") as f:
        data = f.read()
    if args.bit is not None:
        blob = mutate_mod.flip_bit(data, args.byte, args.bit)
        desc = f"翻转 byte={args.byte} bit={args.bit}"
    else:
        value = 0 if args.value is None else args.value
        blob = mutate_mod.replace_byte(data, args.byte, value)
        desc = f"替换 byte={args.byte} -> {value}"
    print(f"变异: {desc}", flush=True)
    try:
        module = decode_module(blob)
    except ToolError as e:
        print("结果: 解码阶段拒绝")
        _print_error(e)
        return 0
    try:
        verify_module(module)
        print("结果: 验证通过（变异未破坏安全性）")
        return 0
    except ToolError as e:
        print("结果: 验证器拒绝")
        _print_error(e)
        return 0


def cmd_mutation_demo(args) -> int:
    """从一个带循环的小程序构造三类代表性变异并逐一验证。

    三类变异严格满足“单字节扰动”：
      1. 回边重定向  —— 回边目标低字节单字节替换为 0（仍在界内，语义改变）；
      2. 越界跳转    —— 挑选一条跳转，其操作数高字节单 bit 翻转后 > 代码长度；
      3. 异常返回    —— int 函数的 RETV(0x51) 单字节替换为 RET(0x52)，
                        或 void main 的 RET 替换为 RETV。
    """
    source = _read_source(args.source)
    module = compile_and_verify(source, args.source)
    blob = module.encode()
    regions = {name: (s, e)
               for name, s, e in mutate_mod.function_regions(blob)}

    # ---- 变异 1+2：选一个含跳转的函数（优先 main） ----
    target_fn = module.by_name("main") or module.functions[0]
    fn_name = target_fn.name
    cstart, cend = regions[fn_name]
    code_len = cend - cstart
    jumps = [ins for ins in target_fn.instructions if ins.opcode in JUMP_OPS]
    if not jumps:
        print("演示程序不含跳转，无法演示回边/越界变异", file=sys.stderr)
        return 2

    results = []

    def trial(title: str, mutant: bytes, n_changed: int) -> None:
        print("=" * 72)
        print(f"{title}（改动 {n_changed} 字节）")
        try:
            m2 = decode_module(mutant)
            verify_module(m2)
            print("  -> 验证通过（变异未破坏安全性）")
            results.append((title, "accepted"))
            return
        except ToolError as e:
            print(f"  -> [{e.phase}/{e.kind}] {e.message}")
            if e.path is not None and getattr(e.path, "steps", None):
                print(render_path(e.path))
            results.append((title, f"{e.phase}:{e.kind}"))

    back = next((i for i in jumps if i.operand <= i.pc), jumps[0])
    mut1 = mutate_mod.replace_byte(blob, cstart + back.pc + 1, 0x00)
    trial(f"变异1 [回边重定向] {fn_name}: 跳转目标低字节 -> 0 (函数入口方向)",
          mut1, 1)

    # 选一个“单 bit 翻转即可越界”的跳转操作数高字节
    oob_mut = None
    for ins in jumps:
        hi_addr = cstart + ins.pc + 2
        hi = blob[hi_addr]
        for bit in range(8):
            new_hi = hi ^ (1 << bit)
            if new_hi * 256 > code_len and new_hi != hi:
                oob_mut = (mutate_mod.flip_bit(blob, hi_addr, bit),
                           ins.pc, new_hi * 256, bit)
                break
        if oob_mut:
            break
    if oob_mut is None:
        # 兜底：直接把高字节置 0xFF（仍是单字节替换）
        ins = jumps[0]
        hi_addr = cstart + ins.pc + 2
        oob_mut = (mutate_mod.replace_byte(blob, hi_addr, 0xFF),
                   ins.pc, 0xFF00, 0)
    mut2, jpc, new_t, bit = oob_mut
    trial(f"变异2 [越界跳转] {fn_name}: offset={jpc} 跳转目标 -> {new_t} "
          f"(代码长度 {code_len})", mut2, 1)

    # ---- 变异 3：异常返回（RETV -> RET） ----
    retv_fn = next((f for f in module.functions
                    if any(i.opcode == 0x51 for i in f.instructions)), None)
    if retv_fn is not None:
        rs, _ = regions[retv_fn.name]
        retv = next(i for i in retv_fn.instructions if i.opcode == 0x51)
        mut3 = mutate_mod.replace_byte(blob, rs + retv.pc, 0x52)
        trial(f"变异3 [异常返回] {retv_fn.name}: RETV -> RET（返回类型不匹配/缺值）",
              mut3, 1)
    else:
        rs, _ = regions[target_fn.name]
        ret = target_fn.instructions[-1]
        mut3 = mutate_mod.replace_byte(blob, rs + ret.pc, 0x51)
        trial("变异3 [异常返回]: RET -> RETV（void 函数带值返回）", mut3, 1)

    print("=" * 72)
    print("汇总:")
    for title, r in results:
        print(f"  {r:<26} {title}")
    return 0


def cmd_serve(args) -> int:
    from .service import serve
    serve(args.host, args.port)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="byteverifier",
                                description="栈式字节码工具链与验证器")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("compile", help="源码 -> 二进制模块")
    sp.add_argument("source")
    sp.add_argument("-o", "--output")
    sp.set_defaults(func=cmd_compile)

    sp = sub.add_parser("verify", help="验证二进制模块")
    sp.add_argument("module")
    sp.set_defaults(func=cmd_verify)

    sp = sub.add_parser("run", help="编译+验证+运行源码")
    sp.add_argument("source")
    sp.add_argument("--fuel", type=int, default=100_000)
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("disasm", help="反汇编（源码或 .bv）")
    sp.add_argument("path")
    sp.set_defaults(func=cmd_disasm)

    sp = sub.add_parser("mutate", help="对模块做单字节变异后验证")
    sp.add_argument("module")
    sp.add_argument("--byte", type=int, required=True)
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--bit", type=int)
    g.add_argument("--value", type=int)
    sp.set_defaults(func=cmd_mutate)

    sp = sub.add_parser("mutation-demo",
                        help="构造回边/越界/异常返回三类代表性变异")
    sp.add_argument("source")
    sp.set_defaults(func=cmd_mutation_demo)

    sp = sub.add_parser("serve", help="启动 JSON HTTP 服务")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8080)
    sp.set_defaults(func=cmd_serve)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except ToolError as e:
        _print_error(e)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
