"""命令行接口。

子命令：
  keygen    本地生成 Ed25519 密钥对，公钥写入信任库，私钥写 PEM（可加密）
  sign      枚举制品目录、计算摘要、签名并输出清单 JSON
  verify    验证签名（可信密钥 ID）、文件摘要、路径安全、多余文件
  run       验证通过后才执行清单中的 entrypoint（失败绝不执行）
  serve     启动仅监听 127.0.0.1 的本地 HTTP 服务
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from . import __version__
from .errors import ManifestError
from .keys import (
    TrustStore,
    generate_private_key,
    load_private_key_pem,
    public_from_private,
    save_private_key_pem,
)
from .manifest import sign_artifact, write_envelope
from .verify import verify_artifact


def _read_password(args: argparse.Namespace) -> str | None:
    if getattr(args, "no_password", False):
        return None
    env_pw = None
    import os

    env_pw = os.environ.get("SIG_MANIFEST_KEY_PASSWORD")
    if env_pw is not None:
        return env_pw
    pw1 = getpass.getpass("私钥口令（留空则不加密）: ")
    if pw1 == "":
        return None
    if getattr(sys.stdin, "isatty", lambda: False)():
        pw2 = getpass.getpass("再次输入口令: ")
        if pw1 != pw2:
            raise ManifestError("两次输入的口令不一致")
    return pw1


def _load_or_create_trust_store(path: Path) -> TrustStore:
    if path.exists():
        return TrustStore.load(path)
    return TrustStore()


def cmd_keygen(args: argparse.Namespace) -> int:
    key_path = Path(args.private_key)
    trust_path = Path(args.trust_store)
    if key_path.exists() and not args.force:
        raise ManifestError(f"私钥文件已存在: {key_path}（--force 可覆盖）")

    private_key = generate_private_key()
    password = _read_password(args)
    save_private_key_pem(private_key, key_path, password)

    stored = public_from_private(private_key)
    store = _load_or_create_trust_store(trust_path)
    if store.contains(stored.key_id) and not args.force:
        # 私钥新生成而公钥撞库在 Ed25519 下不可能，这里只防文件状态竞争。
        raise ManifestError(f"信任库已存在该密钥 ID: {stored.key_id}")
    store.add(stored)
    store.save(trust_path)

    print(f"已生成 Ed25519 私钥: {key_path}")
    if password is None:
        print("警告: 私钥未加密，请仅在本地测试环境使用。", file=sys.stderr)
    print(f"公钥已加入信任库 : {trust_path}")
    print(f"密钥 ID          : {stored.key_id}")
    return 0


def _load_private_key(args: argparse.Namespace):
    import os

    password = os.environ.get("SIG_MANIFEST_KEY_PASSWORD")
    if password is None and not getattr(args, "no_password", False):
        # 非交互场景（如测试重定向 stdin）直接尝试无口令加载；
        # 加密私钥会给出明确报错。
        if getattr(sys.stdin, "isatty", lambda: False)():
            password = getpass.getpass("私钥口令: ")
    return load_private_key_pem(args.private_key, password)


def cmd_sign(args: argparse.Namespace) -> int:
    artifact_root = Path(args.artifact_root)
    if not artifact_root.is_dir():
        raise ManifestError(f"制品根目录不存在或不是目录: {artifact_root}")

    entrypoint: list[str] | None = None
    if args.entrypoint:
        entrypoint = args.entrypoint
    elif args.entrypoint_sh:
        # 便捷形式：--entrypoint-sh script.sh 等价于 [script.sh]
        entrypoint = [args.entrypoint_sh]

    private_key = _load_private_key(args)
    envelope = sign_artifact(
        artifact_root,
        args.name,
        private_key,
        entrypoint=entrypoint,
    )
    out = Path(args.output)
    write_envelope(envelope, out)
    key_id = envelope["signatures"][0]["key_id"]
    print(f"已生成清单: {out}（文件 {len(envelope['signed']['files'])} 个，签名密钥 {key_id}）")
    return 0


def _load_manifest_text(args: argparse.Namespace) -> bytes:
    return Path(args.manifest).read_bytes()


def cmd_verify(args: argparse.Namespace) -> int:
    trust_store = TrustStore.load(args.trust_store)
    manifest_text = _load_manifest_text(args)
    report = verify_artifact(
        args.artifact_root,
        manifest_text,
        trust_store,
        allow_extra_files=args.allow_extra_files,
    )
    print(report.summary())
    return 0 if report.ok else 1


def cmd_run(args: argparse.Namespace) -> int:
    from .runner import verify_then_run

    trust_store = TrustStore.load(args.trust_store)
    manifest_text = _load_manifest_text(args)
    result = verify_then_run(
        args.artifact_root,
        manifest_text,
        trust_store,
        extra_args=args.args,
        timeout=args.timeout,
        clean_env=not args.inherit_env,
        dry_run=args.dry_run,
    )
    if args.dry_run:
        print("DRY RUN：验证通过，未执行子进程")
        print(f"argv: {result.argv}")
        return 0
    return result.returncode


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import create_server

    httpd = create_server(
        host=args.host,
        port=args.port,
        trust_store_path=args.trust_store,
    )
    host, port = httpd.server_address[:2]
    print(f"本地签名制品服务监听 http://{host}:{port}（仅本机访问）", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭服务…", file=sys.stderr)
    finally:
        httpd.server_close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sig-manifest",
        description="离线签名制品清单与验证服务（纯后端，Ed25519 + SHA-256）",
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_keygen = sub.add_parser("keygen", help="生成测试用 Ed25519 密钥对并登记信任库")
    p_keygen.add_argument("--private-key", required=True, help="私钥 PEM 输出路径")
    p_keygen.add_argument("--trust-store", required=True, help="信任库 JSON 路径")
    p_keygen.add_argument("--force", action="store_true", help="允许覆盖已存在文件")
    p_keygen.add_argument("--no-password", action="store_true", help="私钥不加密（仅测试）")
    p_keygen.set_defaults(func=cmd_keygen)

    p_sign = sub.add_parser("sign", help="对制品目录签名，生成清单")
    p_sign.add_argument("--artifact-root", required=True, help="制品根目录")
    p_sign.add_argument("--name", required=True, help="制品名称")
    p_sign.add_argument("--private-key", required=True, help="签名私钥 PEM")
    p_sign.add_argument("--output", required=True, help="清单输出路径")
    p_sign.add_argument("--no-password", action="store_true", help="私钥无口令")
    grp = p_sign.add_mutually_exclusive_group()
    grp.add_argument("--entrypoint", nargs="+", help="入口 argv（程序必须在制品内）")
    grp.add_argument("--entrypoint-sh", help="便捷形式：单个入口程序路径")
    p_sign.set_defaults(func=cmd_sign)

    p_verify = sub.add_parser("verify", help="验证清单与制品目录")
    p_verify.add_argument("--artifact-root", required=True)
    p_verify.add_argument("--manifest", required=True, help="清单 JSON 路径")
    p_verify.add_argument("--trust-store", required=True)
    p_verify.add_argument("--allow-extra-files", action="store_true",
                          help="允许制品中存在清单未记录的文件（默认拒绝）")
    p_verify.set_defaults(func=cmd_verify)

    p_run = sub.add_parser("run", help="验证通过后执行 entrypoint")
    p_run.add_argument("--artifact-root", required=True)
    p_run.add_argument("--manifest", required=True)
    p_run.add_argument("--trust-store", required=True)
    p_run.add_argument("--no-password", action="store_true")
    p_run.add_argument("--timeout", type=float, default=None)
    p_run.add_argument("--inherit-env", action="store_true", help="继承完整宿主环境")
    p_run.add_argument("--dry-run", action="store_true", help="只验证并打印 argv，不执行")
    p_run.add_argument("args", nargs="*", help="追加到 entrypoint 末尾的参数")
    p_run.set_defaults(func=cmd_run)

    p_serve = sub.add_parser("serve", help="启动本地 HTTP 服务")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8080)
    p_serve.add_argument("--trust-store", required=True)
    p_serve.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except ManifestError as exc:
        print(f"错误[{exc.error_code}]: {exc}", file=sys.stderr)
        return exc.cli_exit_code


if __name__ == "__main__":
    sys.exit(main())
