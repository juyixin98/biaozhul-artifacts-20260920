"""命令行工具：既可启动服务，也可作为验证方客户端调用并**本地校验**。

示例见 examples/ 目录与 README。
"""

from __future__ import annotations

import argparse
import json
import sys
from base64 import b64decode

from .client import TransparencyLogClient
from .hashing import leaf_hash
from .merkle import verify_consistency, verify_inclusion


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tlog.cli",
        description="透明日志本地服务与验证客户端（RFC 9162 风格 Merkle 树）",
    )
    parser.add_argument("--url", default="http://127.0.0.1:8080", help="服务地址")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_serve = sub.add_parser("serve", help="启动本地 HTTP 服务")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8080)
    p_serve.add_argument("--data-dir", default="log_data")

    p_add = sub.add_parser("add", help="追加一条叶子（UTF-8 文本）")
    p_add.add_argument("text", help="叶子文本内容")

    p_addb = sub.add_parser("add-b64", help="追加一条叶子（base64 字节）")
    p_addb.add_argument("b64", help="base64 编码的叶子数据")

    sub.add_parser("sth", help="获取签名树头 STH")
    sub.add_parser("size", help="查询当前树大小")

    p_leaf = sub.add_parser("leaf", help="读取一条叶子")
    p_leaf.add_argument("index", type=int)

    p_inc = sub.add_parser("inclusion", help="获取并本地验证包含证明")
    p_inc.add_argument("leaf_index", type=int)
    p_inc.add_argument("--tree-size", type=int, default=None)
    p_inc.add_argument(
        "--expect-root",
        default=None,
        help="钉住的期望树根(hex)；不给定则取服务端当前 STH 的根",
    )
    p_inc.add_argument(
        "--expect-data-b64",
        default=None,
        help="本地持有的叶子数据(base64)；不给定则通过 /get-leaf 获取",
    )

    p_con = sub.add_parser("consistency", help="获取并本地验证一致性证明")
    p_con.add_argument("first_size", type=int)

    return parser


def _print_ok(ok: bool) -> int:
    print("校验结果：", "通过 ✓" if ok else "失败 ✗")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    client = TransparencyLogClient(args.url)

    if args.cmd == "serve":
        from .server import serve

        serve(args.host, args.port, args.data_dir)
        return 0

    if args.cmd == "add":
        print(json.dumps(client.add_leaf_utf8(args.text), ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "add-b64":
        print(
            json.dumps(
                client.add_leaf_bytes(b64decode(args.b64, validate=True)),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.cmd == "size":
        print(json.dumps({"tree_size": client.get_sth()["tree_size"]}, indent=2))
        return 0

    if args.cmd == "sth":
        sth = client.get_sth()
        print(json.dumps(sth, ensure_ascii=False, indent=2))
        return _print_ok(client.verify_sth(sth))

    if args.cmd == "leaf":
        print(json.dumps(client.get_leaf(args.index), ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "inclusion":
        resp = client.get_inclusion_proof(args.leaf_index, args.tree_size)
        print("包含证明响应：")
        print(json.dumps(resp, ensure_ascii=False, indent=2))

        if args.expect_data_b64 is not None:
            leaf_data = b64decode(args.expect_data_b64, validate=True)
        else:
            leaf_data = b64decode(client.get_leaf(args.leaf_index)["data_b64"])

        if args.expect_root is not None:
            expected_root = bytes.fromhex(args.expect_root)
        else:
            expected_root = bytes.fromhex(client.get_sth()["sha256_root_hash"])

        ok = verify_inclusion(
            leaf_hash(leaf_data),
            resp["leaf_index"],
            resp["tree_size"],
            [bytes.fromhex(p) for p in resp["inclusion_path"]],
            expected_root,
        )
        return _print_ok(ok)

    if args.cmd == "consistency":
        resp = client.get_consistency_proof(args.first_size)
        print("一致性证明响应：")
        print(json.dumps(resp, ensure_ascii=False, indent=2))
        ok = verify_consistency(
            resp["first_size"],
            resp["second_size"],
            [bytes.fromhex(p) for p in resp["consistency_path"]],
            bytes.fromhex(resp["first_root_hash_hex"]),
            bytes.fromhex(resp["second_root_hash_hex"]),
        )
        return _print_ok(ok)

    return 2


if __name__ == "__main__":
    sys.exit(main())
