"""命令行接口。

用法示例::

    python -m envelope --store ./data new-key
    python -m envelope --store ./data encrypt secret.txt
    python -m envelope --store ./data decrypt <fid> recovered.txt
    python -m envelope --store ./data rotate <fid>
    python -m envelope --store ./data list
    python -m envelope --store ./data serve --port 8080
"""

from __future__ import annotations

import argparse
import base64
import json
import sys

from .errors import EnvelopeError
from .server import serve
from .service import EnvelopeService

_DEFAULT_STORE = "./envelope-store"


def _b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _print_json(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="envelope",
        description="信封加密 / 主密钥轮换本地服务（AES-256-GCM，仅本地测试用途）",
    )
    parser.add_argument(
        "--store",
        default=_DEFAULT_STORE,
        help=f"本地存储目录（默认 {_DEFAULT_STORE}）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("new-key", help="生成一把新的随机主密钥")
    sub.add_parser("list-keys", help="列出全部主密钥")
    sub.add_parser("list", help="列出全部已加密文件")

    p_enc = sub.add_parser("encrypt", help="加密本地文件")
    p_enc.add_argument("plaintext", help="明文文件路径（加密后不会删除源文件）")
    p_enc.add_argument(
        "--chunk-size", type=int, default=64 * 1024, help="分块大小（字节，默认 64KiB）"
    )

    p_dec = sub.add_parser("decrypt", help="解密到本地文件（临时明文异常即安全删除）")
    p_dec.add_argument("file_id", help="加密时返回的文件 ID")
    p_dec.add_argument("output", help="解密输出路径")

    p_info = sub.add_parser("info", help="查看文件信封信息")
    p_info.add_argument("file_id")

    p_rot = sub.add_parser("rotate", help="把文件的数据密钥重新包裹到新主密钥")
    p_rot.add_argument("file_id")
    p_rot.add_argument(
        "--new-key",
        default=None,
        help="指定已有主密钥 ID；不指定则生成一把新主密钥",
    )

    p_serve = sub.add_parser("serve", help="启动本地 HTTP 服务（仅绑定 127.0.0.1）")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8080)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    svc = EnvelopeService(args.store)

    try:
        if args.command == "new-key":
            entry = svc.create_master_key()
            _print_json({"kid": entry.kid, "created_at": entry.created_at})

        elif args.command == "list-keys":
            entries = svc.list_master_keys()
            latest = entries[-1].kid if entries else None
            _print_json(
                {
                    "keys": [
                        {
                            "kid": e.kid,
                            "created_at": e.created_at,
                            "is_latest": e.kid == latest,
                        }
                        for e in entries
                    ]
                }
            )

        elif args.command == "list":
            _print_json({"files": [svc.describe(fid) for fid in svc.list_files()]})

        elif args.command == "encrypt":
            loc = svc.encrypt_file(args.plaintext, chunk_size=args.chunk_size)
            info = svc.describe(loc.file_id)
            _print_json(
                {
                    "file_id": loc.file_id,
                    "plaintext_size": info["plaintext_size"],
                    "chunk_size": info["chunk_size"],
                    "blocks": info["blocks"],
                    "wrapped_by_kid": info["wrapped_by_kid"],
                    "meta_path": str(loc.meta_path),
                    "blob_path": str(loc.blob_path),
                }
            )

        elif args.command == "decrypt":
            out = svc.decrypt_file(args.file_id, args.output)
            _print_json({"file_id": args.file_id, "output": str(out)})

        elif args.command == "info":
            _print_json(svc.describe(args.file_id))

        elif args.command == "rotate":
            info = svc.rotate_master_key(args.file_id, new_kid=args.new_key)
            _print_json(
                {
                    "file_id": info.file_id,
                    "old_kid": info.old_kid,
                    "new_kid": info.new_kid,
                    "header_version": info.header_version,
                    "blob_bytes_changed": info.blob_bytes_changed,
                }
            )

        elif args.command == "serve":
            serve(args.store, host=args.host, port=args.port, verbose=True)

    except EnvelopeError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except (FileNotFoundError, FileExistsError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
