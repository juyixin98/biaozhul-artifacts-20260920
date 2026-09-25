"""命令行入口。

快速匹配：
    python -m renfa.cli -p 'a(b|c)*' -t abbc --op search
启动 JSON 服务：
    python -m renfa.cli serve --port 8080
"""

import argparse
import json
import sys

from .errors import RegexError
from .service import serve
from .engine import Regex


def _print_match(regex: Regex, text: str, op: str) -> int:
    if op == "findall":
        ms = regex.findall(text)
        print(json.dumps(
            {"matched": bool(ms), "matches": [
                {"start": m.start, "end": m.end, "text": m.text} for m in ms]},
            ensure_ascii=False, indent=2))
        return 0
    m = getattr(regex, op)(text)
    print(json.dumps(
        {"matched": m is not None,
         "match": None if m is None
                  else {"start": m.start, "end": m.end, "text": m.text}},
        ensure_ascii=False, indent=2))
    return 0 if m is not None else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="renfa", description="正则自动机引擎")
    sub = ap.add_subparsers(dest="command")

    m = sub.add_parser("match", help="在命令行直接做一次匹配")
    m.add_argument("-p", "--pattern", required=True)
    m.add_argument("-t", "--text", required=True)
    m.add_argument("--op", choices=["fullmatch", "match", "search", "findall"],
                   default="search")

    s = sub.add_parser("serve", help="启动 JSON 服务")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8080)
    s.add_argument("--verbose", action="store_true")

    args = ap.parse_args(argv)
    if args.command is None:
        ap.print_help()
        return 2
    if args.command == "serve":
        serve(args.host, args.port, args.verbose)
        return 0
    try:
        regex = Regex(args.pattern)
    except RegexError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 2
    return _print_match(regex, args.text, args.op)


if __name__ == "__main__":
    raise SystemExit(main())
