"""命令行入口：``python -m dms ...`` 或安装后的 ``dms``。

子命令：

* ``keygen [--path FILE]``                 生成本地测试主密钥（0600）
* ``serve [--host H] [--port P]``          启动本地 HTTP 服务
* ``mask --rules R.json --doc D.json``     一次性编译+脱敏，结果写到 stdout
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .crypto import CryptoProvider
from .engine import apply_rules
from .errors import DMSError
from .logging_utils import get_logger
from .rules import compile_rules
from .server import serve

logger = get_logger("dms.cli")


def _load_json(path: str) -> object:
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise DMSError(f"无法读取文件：{path}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DMSError(f"文件不是合法 JSON：{path}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dms", description="本地 JSON 脱敏规则编译器")
    sub = parser.add_subparsers(dest="command", required=True)

    kg = sub.add_parser("keygen", help="生成本地测试主密钥")
    kg.add_argument("--path", default=".secrets/dms-test-key.json")

    sv = sub.add_parser("serve", help="启动本地 HTTP 服务")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8080)
    sv.add_argument("--key-file", default=".secrets/dms-test-key.json")

    mk = sub.add_parser("mask", help="一次性脱敏（规则+文档 -> stdout）")
    mk.add_argument("--rules", required=True)
    mk.add_argument("--doc", required=True)
    mk.add_argument("--key-file", default=".secrets/dms-test-key.json")

    args = parser.parse_args(argv)

    try:
        if args.command == "keygen":
            path = CryptoProvider.generate().save(args.path)
            print(f"已生成测试主密钥：{path}（权限 0600，请勿用于生产）")
            return 0
        if args.command == "serve":
            serve(args.host, args.port, args.key_file)
            return 0
        if args.command == "mask":
            rules_spec = _load_json(args.rules)
            document = _load_json(args.doc)
            compiled = compile_rules(rules_spec)
            crypto = (
                CryptoProvider.load_or_create(args.key_file)
                if compiled.needs_crypto
                else None
            )
            result = apply_rules(compiled, document, crypto)
            print(json.dumps({"document": result.document, "stats": result.stats},
                             ensure_ascii=False, indent=2, allow_nan=False))
            return 0
    except DMSError as exc:
        # 错误输出只含 schema 信息
        print(json.dumps({"ok": False, "error": {"code": exc.code,
                                                 "message": str(exc),
                                                 "details": exc.details}},
                         ensure_ascii=False), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
