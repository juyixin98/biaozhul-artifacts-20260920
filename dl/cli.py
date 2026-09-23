"""命令行工具：全量解析 / 词法切分 / 单次编辑的增量解析演示。"""

from __future__ import annotations

import argparse
import json
import sys

from .diff import result_to_dict
from .incremental import Document, Edit
from .lexer import tokenize
from .parser import parse


def main(argv: list[str] | None = None):
    ap = argparse.ArgumentParser(description="dlang 解析工具链命令行")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_parse = sub.add_parser("parse", help="全量解析文件并输出 JSON")
    p_parse.add_argument("file")
    p_parse.add_argument("--tokens", action="store_true",
                         help="附带令牌流")

    p_lex = sub.add_parser("lex", help="只做词法分析")
    p_lex.add_argument("file")

    p_edit = sub.add_parser("edit", help="单次编辑的增量解析演示")
    p_edit.add_argument("file")
    p_edit.add_argument("--start", type=int, required=True)
    p_edit.add_argument("--end", type=int, required=True)
    p_edit.add_argument("--text", default="")

    args = ap.parse_args(argv)

    with open(args.file, encoding="utf-8") as f:
        source = f.read()

    if args.cmd == "parse":
        result = parse(source)
        print(json.dumps(result_to_dict(result, include_tokens=args.tokens),
                         ensure_ascii=False, indent=2))
        return 1 if result.errors else 0

    if args.cmd == "lex":
        tokens, errors = tokenize(source)
        print(json.dumps(
            {"tokens": [t.to_dict() for t in tokens],
             "errors": [e.to_dict() for e in errors]},
            ensure_ascii=False, indent=2))
        return 1 if errors else 0

    if args.cmd == "edit":
        doc = Document(source=source)
        before_ids = {n.node_id for n in _walk(doc.result.tree)}
        doc.apply_edit(Edit(args.start, args.end, args.text))
        after_ids = {n.node_id for n in _walk(doc.result.tree)}
        out = result_to_dict(doc.result)
        out["reuse_events"] = doc.reuse_events
        out["reused_node_ids"] = sorted(before_ids & after_ids)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 1 if doc.result.errors else 0


def _walk(node):
    yield node
    for c in node.children:
        yield from _walk(c)


if __name__ == "__main__":
    sys.exit(main())
