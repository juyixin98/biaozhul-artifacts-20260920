"""slang 命令行入口。

用法
====
    python -m slang compile <src.sl> [-o module.bin] [--encoding hex]
    python -m slang verify <module.bin | ->          (默认 hex 文本 stdin)
    python -m slang run <src.sl | module.bin> [--fuel N]
    python -m slang disassemble <module.bin>
    python -m slang mutate <src.sl | module.bin>
                      [--strategy targeted|exhaustive]
                      [--func INDEX] [--fuel N] [--json] [--limit N]
    python -m slang serve [--host 127.0.0.1] [--port 8000]

模块二进制默认用 hex 文本传输（.bin 文件里放纯 hex 字符串），
也可用 --raw 指定原始字节。
"""

from __future__ import annotations

import argparse
import json
import sys

from . import VERSION
from .bytecode import decode_module
from .campaign import run_campaign
from .compiler import compile_source
from .errors import CompileError, DecodeError, RuntimeErr
from .interpreter import Interpreter, format_value
from .verifier import verify_module


def _read_module_arg(arg: str, raw: bool):
    """arg 可以是 .sl 源文件（先编译）或模块 hex 文件。"""
    with open(arg, "r", encoding="utf-8") as f:
        text = f.read()
    if arg.endswith(".sl"):
        return compile_source(text, filename=arg)
    data = bytes.fromhex(text.strip()) if not raw else _read_raw(arg)
    return decode_module(data)


def _read_raw(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _module_from_input(path: str | None, raw: bool):
    if path is None or path == "-":
        text = sys.stdin.read()
        return decode_module(bytes.fromhex(text.strip()) if not raw
                             else sys.stdin.buffer.read())
    return _read_module_arg(path, raw)


def cmd_compile(a: argparse.Namespace) -> int:
    with open(a.source, "r", encoding="utf-8") as f:
        text = f.read()
    try:
        module = compile_source(text, filename=a.source, module_name=a.name)
    except CompileError as e:
        from .location import SourceText
        sys.stderr.write(e.render(SourceText(text, a.source)) + "\n")
        return 2
    binary = module.encode()
    if a.output:
        with open(a.output, "wb" if a.raw else "w", encoding=None if a.raw else "utf-8") as f:
            if a.raw:
                f.write(binary)
            else:
                f.write(binary.hex())
        print(f"已写出 {a.output}（{len(binary)} 字节，{len(module.funcs)} 个函数）")
    else:
        print(binary.hex())
    return 0


def _print_verify_error(err, source_text: str | None) -> None:
    head = f"[{err.code}] 函数 {err.function_name} (#{err.function_index})"
    if err.pc >= 0:
        head += f" pc={err.pc}"
    if err.src_line:
        head += f"  源码 {err.src_line}:{err.src_col}"
    print(head)
    print(f"  {err.message}")
    print("  最短错误路径:")
    print(err.render_path())


def cmd_verify(a: argparse.Namespace) -> int:
    try:
        module = _module_from_input(a.module, a.raw)
    except DecodeError as e:
        print(f"[DECODE_ERROR] {e}")
        return 1
    errors = verify_module(module)
    if not errors:
        print(f"验证通过：{len(module.funcs)} 个函数")
        return 0
    for e in errors:
        _print_verify_error(e, module.source)
        print()
    return 1


def cmd_run(a: argparse.Namespace) -> int:
    # run 接受源文件（先编译）或模块
    if a.target.endswith(".sl"):
        with open(a.target, "r", encoding="utf-8") as f:
            text = f.read()
        try:
            module = compile_source(text, filename=a.target)
        except CompileError as e:
            from .location import SourceText
            sys.stderr.write(e.render(SourceText(text, a.target)) + "\n")
            return 2
    else:
        module = _module_from_input(a.target, a.raw)
    errs = verify_module(module)
    if errs:
        for e in errs:
            _print_verify_error(e, module.source)
        return 1
    try:
        result = Interpreter(module, fuel=a.fuel).run_main()
    except RuntimeErr as e:
        print(f"运行期错误: {e}", file=sys.stderr)
        return 3
    for line in result.printed:
        print(line)
    print(f"[main 返回 {format_value(result.return_value)}, "
          f"{result.steps} 步, 最大栈高 {result.max_stack}]",
          file=sys.stderr)
    return 0


def cmd_disasm(a: argparse.Namespace) -> int:
    module = _module_from_input(a.module, a.raw)
    for fi, fn in enumerate(module.funcs):
        print(f"; function #{fi} {fn.name}  params={fn.param_types} "
              f"ret={fn.ret_type} locals={fn.local_types} codelen={len(fn.code)}")
        for ins in fn.decode():
            tgt = f" -> {ins.target()}" if ins.op in (0x15, 0x16) else ""
            operand = f" {ins.operand}" if ins.operand is not None else ""
            src = ""
            d = fn.debug_at(ins.pc)
            if d and module.source:
                line = module.source.count("\n", 0, d.src_start) + 1
                src = f"   ; line {line}"
            print(f"  {ins.pc:4d}: {ins.name:<7}{operand}{tgt}{src}")
        print()
    return 0


def cmd_mutate(a: argparse.Namespace) -> int:
    module = _read_module_arg(a.target, a.raw)
    # 先确认原始程序自身通过验证（变异基线必须是合法程序）
    base = verify_module(module)
    if base:
        print("基线模块未通过验证，拒绝变异：", file=sys.stderr)
        for e in base:
            _print_verify_error(e, module.source)
        return 2
    report = run_campaign(module, strategy=a.strategy,
                          func_index=a.func_index, fuel=a.fuel)
    if a.json:
        payload = {k: v for k, v in report.items() if k != "outcomes"}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"策略 {report['strategy']}，共 {report['total']} 个单字节变异")
        for cat, n in sorted(report["by_category"].items()):
            print(f"  {cat:20s} {n}")
        print("验证拒绝按错误码:")
        for code, n in sorted(report["verify_error_codes"].items()):
            print(f"  {code:24s} {n}")
        print("\n各类别代表样本:")
        for cat, ex in report["examples"].items():
            print(f"  == {cat} ==")
            print(f"    变异: {ex['mutation']}")
            if ex["error_code"]:
                print(f"    错误: [{ex['error_code']}] {ex['message']}")
            if ex["path"]:
                for i, node in enumerate(ex["path"]):
                    print(f"    {node}")
    inv = report["by_category"].get("invariant_broken", 0)
    crashes = report["by_category"].get("verifier_crashed", 0) + \
        report["by_category"].get("interpreter_crashed", 0)
    return 4 if (inv or crashes) else 0


def cmd_serve(a: argparse.Namespace) -> int:
    from .service import serve
    httpd = serve(a.host, a.port, verbose=a.verbose)
    print(f"slang JSON 服务监听 http://{a.host}:{a.port}", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n关闭", file=sys.stderr)
    finally:
        httpd.server_close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="slang",
                                description="小语言栈式字节码工具链与验证器")
    p.add_argument("--version", action="version", version=f"slang {VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)

    pc = sub.add_parser("compile", help="源码 -> 模块字节码(hex)")
    pc.add_argument("source")
    pc.add_argument("-o", "--output")
    pc.add_argument("--name", default="main")
    pc.add_argument("--raw", action="store_true", help="输出原始字节而非 hex")
    pc.set_defaults(func=cmd_compile)

    pv = sub.add_parser("verify", help="验证模块")
    pv.add_argument("module", nargs="?", default="-")
    pv.add_argument("--raw", action="store_true")
    pv.set_defaults(func=cmd_verify)

    pr = sub.add_parser("run", help="验证并运行 .sl 源文件或模块")
    pr.add_argument("target")
    pr.add_argument("--fuel", type=int, default=1_000_000)
    pr.add_argument("--raw", action="store_true")
    pr.set_defaults(func=cmd_run)

    pd = sub.add_parser("disassemble", help="反汇编模块")
    pd.add_argument("module", nargs="?", default="-")
    pd.add_argument("--raw", action="store_true")
    pd.set_defaults(func=cmd_disasm)

    pm = sub.add_parser("mutate", help="单字节变异活动")
    pm.add_argument("target")
    pm.add_argument("--strategy", choices=["targeted", "exhaustive"],
                    default="targeted")
    pm.add_argument("--func", type=int, default=None, dest="func_index")
    pm.add_argument("--fuel", type=int, default=20_000)
    pm.add_argument("--json", action="store_true")
    pm.add_argument("--limit", type=int, default=None)
    pm.add_argument("--raw", action="store_true")
    pm.set_defaults(func=cmd_mutate)

    ps = sub.add_parser("serve", help="启动 JSON HTTP 服务")
    ps.add_argument("--host", default="127.0.0.1")
    ps.add_argument("--port", type=int, default=8000)
    ps.add_argument("--verbose", action="store_true")
    ps.set_defaults(func=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
