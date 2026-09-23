"""命令行入口。

用法::

    python -m constprop.cli parse    FILE [-o OUT.json]
    python -m constprop.cli ir       FILE            # 优化前 SSA IR
    python -m constprop.cli analyze  FILE            # SCCP 格结果
    python -m constprop.cli optimize FILE [-o DIR]   # 分析+优化+前后运行对比
    python -m constprop.cli run      FILE            # 用 AST 金标准执行
    python -m constprop.cli serve [--host H] [--port P]
"""

from __future__ import annotations

import argparse
import json
import sys

from .cfg import build_cfg
from .interp import run_ast
from .ir_interp import run_ir
from .optimizer import optimize
from .parser import parse_source
from .serialize import ast_to_dict
from .service import DEFAULT_STEP_LIMIT, serve
from .source import L0Error, SourceText
from .sccp import run_sccp
from .ssa import construct_ssa


def _read(path: str) -> tuple[str, SourceText]:
    with open(path, encoding="utf-8") as f:
        text = f.read()
    return text, SourceText(text, path)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="constprop",
                                description="L0 常量传播格分析工具链")
    p.add_argument("--step-limit", type=int, default=DEFAULT_STEP_LIMIT)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("parse", help="词法+语法分析，打印 AST JSON").add_argument("file")
    sub.add_parser("ir", help="构造 SSA IR").add_argument("file")
    sub.add_parser("analyze", help="运行 SCCP 格分析").add_argument("file")
    opt = sub.add_parser("optimize", help="SCCP + 优化 + 前后等价性验证")
    opt.add_argument("file")
    opt.add_argument("--json", action="store_true", help="只输出 JSON")
    sub.add_parser("run", help="按 AST 金标准语义执行").add_argument("file")
    sp = sub.add_parser("serve", help="启动 JSON HTTP 服务")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8000)
    sp.add_argument("--verbose", action="store_true")

    args = p.parse_args(argv)

    if args.cmd == "serve":
        serve(args.host, args.port, args.step_limit, args.verbose)
        return 0

    try:
        text, source = _read(args.file)
        program = parse_source(source)

        if args.cmd == "parse":
            print(json.dumps(ast_to_dict(program), ensure_ascii=False, indent=2))
            return 0

        cfg = build_cfg(program, source)
        construct_ssa(cfg)

        if args.cmd == "ir":
            print(cfg.to_text())
            return 0

        if args.cmd == "analyze":
            result = run_sccp(cfg)
            print("reachable blocks :", sorted(result.reachable))
            print("executable edges :",
                  sorted(result.executable_edges))
            print("lattice (non-top):")
            for name, lv in sorted(result.lattice.items()):
                if lv.kind != "top":
                    print(f"  {name:<12} = {lv}")
            return 0

        if args.cmd == "run":
            res = run_ast(program, source, step_limit=args.step_limit)
            return _print_run(res)

        if args.cmd == "optimize":
            before_ast = run_ast(program, source, step_limit=args.step_limit)
            before_ir = run_ir(cfg, source, step_limit=args.step_limit)
            _, report = optimize(cfg)
            after = run_ir(cfg, source, step_limit=args.step_limit)
            if args.json:
                payload = {
                    "ir_before_text": None,
                    "ir_after_text": cfg.to_text(),
                    "report": report.to_dict(),
                    "run_before_ast": before_ast.to_dict(),
                    "run_before_ir": before_ir.to_dict(),
                    "run_after_ir": after.to_dict(),
                    "equivalent": before_ast.signature() == after.signature(),
                    "ir_unoptimized_matches_ast":
                        before_ast.signature() == before_ir.signature(),
                }
                print(json.dumps(payload, ensure_ascii=False, indent=2))
                return 0

            print("== SCCP 常量 ==")
            for name, n in report.constants_before.items():
                print(f"  {name:<12} = {n}")
            print("\n== 优化改写 ==")
            print(json.dumps(report.to_dict()["changes"], ensure_ascii=False,
                             indent=2))
            print("\n== 优化后 IR ==")
            print(cfg.to_text())
            print("\n== 运行对比 ==")
            print(f"  AST 金标准   : {_fmt_run(before_ast)}")
            print(f"  优化前 IR    : {_fmt_run(before_ir)}")
            print(f"  优化后 IR    : {_fmt_run(after)}")
            equiv = before_ast.signature() == after.signature()
            ir_match = before_ast.signature() == before_ir.signature()
            print(f"\n  未优化IR==AST: {ir_match}")
            print(f"  优化保持等价 : {equiv}")
            return 0 if equiv and ir_match else 2
    except L0Error as e:
        print(e.render(), file=sys.stderr)
        return 1
    return 0


def _fmt_run(res) -> str:
    if res.ok:
        return f"OK  output={res.output}"
    return (f"ERROR {res.error_code} at {res.location} "
            f"output_before_error={res.output}")


def _print_run(res) -> int:
    if res.ok:
        print(res.output_text)
        return 0
    print(res.output_text)
    print(f"runtime error: {res.error_message} ({res.error_code}) "
          f"at {res.location}", file=sys.stderr)
    return 3


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
