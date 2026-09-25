"""命令行入口::

    python -m taintflow.cli analyze FILE [--json] [--ir] [--entry NAME]
                                         [--source NAME]... [--sink NAME]...
                                         [--sanitizer NAME]... [--context-k K]
    python -m taintflow.cli serve [--host 127.0.0.1] [--port 8000]

analyze 默认打印人类可读报告；``--json`` 输出与 HTTP 服务相同的 JSON。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from . import __version__
from .config import AnalysisConfig
from .service import analyze_source, serve


def _config_from_args(args: argparse.Namespace) -> AnalysisConfig:
    d: dict = {"context_k": args.context_k, "max_trace": args.max_trace}
    if args.source:
        d["sources"] = args.source
    if args.sink:
        d["sinks"] = args.sink
    if args.sanitizer:
        d["sanitizers"] = args.sanitizer
    return AnalysisConfig.from_dict(d)


def _print_human(result: dict) -> None:
    if not result["ok"]:
        err = result["error"]
        print(f"[失败] {err['type']}: {err['message']}", file=sys.stderr)
        if err.get("span"):
            print(f"  位置: {err['span']}", file=sys.stderr)
        return
    s = result["summary"]
    print(f"taintflow v{result['version']}  入口={result['entry'] or '(无 main)'}")
    print(
        f"告警 {s['alerts']} 条（确定 {s['true_positives']} / "
        f"可能误报 {s['possible_false_positives']}）"
        f"  摘要 {s['function_contexts']} 个，重分析 {s['reanalyses']} 轮，"
        f"过程内迭代 {s['intra_iterations']} 次"
    )
    if s.get("note"):
        print(f"注意: {s['note']}")
    for i, a in enumerate(result["alerts"], 1):
        tag = "可能误报" if a["may_be_false_positive"] else "告警"
        sp, sk = a["source_point"], a["sink"]
        print(f"\n[{i}] {tag}  {sp['function']}:{sp['line']}:{sp['col']} "
              f"source()  →  {sk['function']}:{sk['line']}:{sk['col']} {sk['name']}()")
        if a["classification_note"]:
            print(f"    说明: {a['classification_note']}")
        if a["context"]:
            print(f"    上下文: {' -> '.join(a['context'])}")
        for st in a["path"]:
            extra = f"  [{st['detail']}]" if st.get("detail") else ""
            print(f"      {st['at']:>24}  {st['kind']:<14} {st['desc']}{extra}")
    for w in result.get("warnings", []):
        print(f"\n[警告:{w['kind']}] {w['span']}: {w['message']}")
    if result.get("ir"):
        print("\n===== IR =====")
        print(json.dumps(result["ir"], ensure_ascii=False, indent=2))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="taintflow", description="小语言跨函数污点分析")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    pa = sub.add_parser("analyze", help="分析源文件")
    pa.add_argument("file")
    pa.add_argument("--json", action="store_true", help="输出 JSON")
    pa.add_argument("--ir", action="store_true", help="附带输出 IR")
    pa.add_argument("--entry", default="main")
    pa.add_argument("--source", action="append", help="自定义污点源名（可多次）")
    pa.add_argument("--sink", action="append", help="自定义汇名（可多次）")
    pa.add_argument("--sanitizer", action="append", help="自定义清洗器名（可多次）")
    pa.add_argument("--context-k", type=int, default=2, help="调用串长度 k（默认 2）")
    pa.add_argument("--max-trace", type=int, default=40)

    ps = sub.add_parser("serve", help="启动 JSON HTTP 服务")
    ps.add_argument("--host", default="127.0.0.1")
    ps.add_argument("--port", type=int, default=8000)

    args = parser.parse_args(argv)
    if args.cmd == "serve":
        serve(args.host, args.port)
        return 0

    try:
        with open(args.file, "r", encoding="utf-8") as f:
            source = f.read()
    except OSError as exc:
        print(f"无法读取文件: {exc}", file=sys.stderr)
        return 2

    cfg = _config_from_args(args)
    result = analyze_source(source, cfg, filename=args.file,
                            entry=args.entry, include_ir=args.ir)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _print_human(result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
