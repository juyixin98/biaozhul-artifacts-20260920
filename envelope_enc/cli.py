"""命令行接口：密钥环管理、加密、解密、轮换、查看头部。

用法示例::

    python -m envelope_enc.cli keyring-init -k keys.json
    python -m envelope_enc.cli encrypt -k keys.json -i plain.bin -o plain.bin.enc
    python -m envelope_enc.cli rotate-master -k keys.json
    python -m envelope_enc.cli rotate -k keys.json plain.bin.enc
    python -m envelope_enc.cli decrypt -k keys.json -i plain.bin.enc -o out.bin
"""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .container import (
    DEFAULT_CHUNK_SIZE,
    ContainerError,
    decrypt_file,
    encrypt_file,
    read_header,
    rotate_file,
    rotate_many,
)
from .keyring import Keyring, KeyringError


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2

    keyring = Keyring(args.keyring) if getattr(args, "keyring", None) else None
    try:
        return args.func(args, keyring)
    except (ContainerError, KeyringError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


# ------------------------------------------------------------------ 子命令实现
def cmd_keyring_init(args, keyring: Keyring) -> int:
    mk = keyring.initialize(exist_ok=args.exist_ok)
    print(f"已初始化密钥环 {args.keyring}")
    print(f"active 主密钥：{mk.kid}（created_at={mk.created_at}）")
    return 0


def cmd_keyring_ls(args, keyring: Keyring) -> int:
    active = keyring.active()
    for mk in keyring.list_keys():
        marker = "* active " if mk.kid == active.kid else f"  {mk.status:<7}"
        print(f"{marker} {mk.kid}  created_at={mk.created_at}")
    return 0


def cmd_rotate_master(args, keyring: Keyring) -> int:
    old = keyring.active()
    new = keyring.rotate_master()
    print(f"主密钥已轮换：{old.kid} -> {new.kid}")
    print(f"旧主密钥 {old.kid} 已置为 retired（仍可解密/轮换旧文件）")
    return 0


def cmd_delete_key(args, keyring: Keyring) -> int:
    keyring.delete_key(args.kid)
    print(f"已删除主密钥 {args.kid}")
    print("警告：仍由该密钥包裹的文件今后将无法解密。")
    return 0


def cmd_encrypt(args, keyring: Keyring) -> int:
    encrypt_file(
        args.input,
        args.output,
        keyring,
        chunk_size=args.chunk_size,
    )
    print(f"已加密：{args.input} -> {args.output}（kid={keyring.active().kid}）")
    return 0


def cmd_decrypt(args, keyring: Keyring) -> int:
    decrypt_file(args.input, args.output, keyring)
    print(f"已解密：{args.input} -> {args.output}")
    return 0


def cmd_rotate(args, keyring: Keyring) -> int:
    results = rotate_many(args.paths, keyring, new_kid=args.new_kid)
    failed = 0
    for r in results:
        if r.ok:
            o = r.outcome
            if o.skipped:
                print(f"跳过（已是 {o.new_kid}）：{r.path}")
            else:
                print(f"已轮换：{r.path}  {o.old_kid} -> {o.new_kid}")
        else:
            failed += 1
            print(f"失败：{r.path}：{r.error}", file=sys.stderr)
    return 1 if failed else 0


def cmd_inspect(args, keyring) -> int:
    header = read_header(args.path)
    for name in (
        "v", "file_id", "kid", "chunk_size",
        "plaintext_size", "chunk_count",
    ):
        print(f"{name:16}{header[name]}")
    return 0


# ------------------------------------------------------------------ 参数解析
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m envelope_enc.cli",
        description="信封加密轮换本地服务（AES-256-GCM，本地测试密钥，禁止用于生产）",
    )
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command")

    # 除 inspect 外所有子命令都需要密钥环；-k 统一放在子命令之后
    kr_parent = argparse.ArgumentParser(add_help=False)
    kr_parent.add_argument(
        "-k", "--keyring", metavar="KEYS_JSON", required=True,
        help="本地密钥环 JSON 文件路径",
    )
    chunk_parent = argparse.ArgumentParser(add_help=False)
    chunk_parent.add_argument(
        "-c", "--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE,
        help=f"块大小（字节），默认 {DEFAULT_CHUNK_SIZE}",
    )

    sp = sub.add_parser(
        "keyring-init", parents=[kr_parent], help="创建密钥环并生成第一把主密钥"
    )
    sp.add_argument("--exist-ok", action="store_true", help="已存在则不报错")
    sp.set_defaults(func=cmd_keyring_init)

    sp = sub.add_parser("keyring-ls", parents=[kr_parent], help="列出主密钥")
    sp.set_defaults(func=cmd_keyring_ls)

    sp = sub.add_parser(
        "rotate-master", parents=[kr_parent],
        help="生成新 active 主密钥，旧的转 retired",
    )
    sp.set_defaults(func=cmd_rotate_master)

    sp = sub.add_parser("delete-key", parents=[kr_parent],
                        help="彻底删除一把（非 active）主密钥")
    sp.add_argument("kid")
    sp.set_defaults(func=cmd_delete_key)

    sp = sub.add_parser("encrypt", parents=[kr_parent, chunk_parent], help="加密文件")
    sp.add_argument("-i", "--input", required=True)
    sp.add_argument("-o", "--output", required=True)
    sp.set_defaults(func=cmd_encrypt)

    sp = sub.add_parser("decrypt", parents=[kr_parent], help="解密文件")
    sp.add_argument("-i", "--input", required=True)
    sp.add_argument("-o", "--output", required=True)
    sp.set_defaults(func=cmd_decrypt)

    sp = sub.add_parser(
        "rotate", parents=[kr_parent],
        help="把一个或多个 .enc 文件轮换到当前 active 主密钥",
    )
    sp.add_argument("paths", nargs="+")
    sp.add_argument("--new-kid", default=None, help="指定目标主密钥（默认 active）")
    sp.set_defaults(func=cmd_rotate)

    sp = sub.add_parser("inspect", help="只解析并打印容器头部（不需要密钥环）")
    sp.add_argument("path")
    sp.set_defaults(func=cmd_inspect)

    return p


if __name__ == "__main__":
    raise SystemExit(main())
