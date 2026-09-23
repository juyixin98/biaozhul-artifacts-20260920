"""tinyinfer 命令行。

用法::

    python -m tinyinfer.cli infer FILE [--naive] [--no-eval] [--trace] [--json]
    python -m tinyinfer.cli parse FILE
    python -m tinyinfer.cli serve [--host H] [--port P]

``--naive`` 关闭值限制（naive 算法 W），用于复现可变引用不健全反例。
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .errors import TinyError
from .pipeline import analyze, parse_source
from .server import build_server

# 轨迹中文标题
_TRACE_TITLES = {
    "fresh": "新变量",
    "bind": "绑定",
    "unify": "合一",
    "generalize": "一般化",
    "no-generalize": "禁止一般化",
    "instantiate": "实例化",
    "fun": "函数",
    "let-rec": "递归绑定",
    "seq": "序列",
}


def _print_trace(trace: list[dict[str, Any]]) -> None:
    print("── 类型推导过程 ──")
    for i, ev in enumerate(trace, 1):
        pos = ""
        if ev.get("span"):
            s = ev["span"]["start"]
            pos = f"  [{s['line']}:{s['column']}]"
        print(f"{i:>3}. {_TRACE_TITLES.get(ev['kind'], ev['kind']):<8}"
              f"{ev['detail']}{pos}")


def _cmd_infer(args: argparse.Namespace) -> int:
    source = _read_input(args.file)
    payload = analyze(
        source,
        value_restriction=not args.naive,
        annotate=not args.no_annotations,
        evaluate=not args.no_eval,
        filename=args.file,
    )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload["ok"] else 1

    if not payload["ok"]:
        err = payload["error"]
        loc = ""
        if err["span"]:
            s = err["span"]["start"]
            loc = f"  ({s['line']}:{s['column']})"
        print(f"[{err['kind']}] {err['message']}{loc}", file=sys.stderr)
        if err.get("snippet"):
            print(err["snippet"], file=sys.stderr)
        return 1

    print(f"最终表达式类型: {payload['type']}")
    if payload["bindings"]:
        print("顶层绑定:")
        for b in payload["bindings"]:
            print(f"  {b['name']} : {b['scheme']}")
    if payload.get("value") is not None:
        print(f"求值结果: {payload['value']}")
    if payload.get("eval_error"):
        ee = payload["eval_error"]
        print(f"[求值期错误 {ee['kind']}] {ee['message']}", file=sys.stderr)
        if ee.get("snippet"):
            print(ee["snippet"], file=sys.stderr)
    if args.trace:
        print()
        _print_trace(payload["trace"])
    return 0


def _cmd_parse(args: argparse.Namespace) -> int:
    source = _read_input(args.file)
    try:
        program = parse_source(source, filename=args.file)
    except TinyError as err:
        print(err.render(source), file=sys.stderr)
        return 1
    print(json.dumps(_program_summary(program), ensure_ascii=False, indent=2))
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    httpd = build_server(args.host, args.port)
    print(f"tinyinfer 服务监听 http://{args.host}:{args.port}", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


def _read_input(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _program_summary(program: Any) -> dict:
    return {
        "top_level_lets": [
            {"name": b.name, "rec": b.rec,
             "annotated": b.ann is not None}
            for b in program.bindings
        ],
        "has_final_expr": program.final_expr is not None,
        "node_count": _count_nodes(program),
    }


def _count_nodes(program: Any) -> int:
    from . import ast

    count = 0

    def walk(e: ast.Expr) -> None:
        nonlocal count
        count += 1
        for child in e.__dict__.values():
            if isinstance(child, ast.Expr):
                walk(child)

    for b in program.bindings:
        walk(b.bound)
    if program.final_expr is not None:
        walk(program.final_expr)
    return count


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tinyinfer",
                                description="带 let 多态的类型推导工具链")
    sub = p.add_subparsers(dest="command", required=True)

    pi = sub.add_parser("infer", help="类型推导并（可选）求值")
    pi.add_argument("file", help="源文件，- 表示标准输入")
    pi.add_argument("--naive", action="store_true",
                    help="关闭值限制（naive 算法 W），用于复现不健全反例")
    pi.add_argument("--no-eval", action="store_true", help="只推导，不求值")
    pi.add_argument("--no-annotations", action="store_true",
                    help="忽略源码中的类型标注")
    pi.add_argument("--trace", action="store_true", help="打印推导过程")
    pi.add_argument("--json", action="store_true", help="输出服务同款 JSON")
    pi.set_defaults(func=_cmd_infer)

    pp = sub.add_parser("parse", help="只做词法/语法分析并输出结构摘要")
    pp.add_argument("file")
    pp.set_defaults(func=_cmd_parse)

    ps = sub.add_parser("serve", help="启动 JSON HTTP 服务")
    ps.add_argument("--host", default="127.0.0.1")
    ps.add_argument("--port", type=int, default=8000)
    ps.set_defaults(func=_cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
