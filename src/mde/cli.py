"""命令行工具（离线可用）。

子命令
======
* ``keygen``   本地生成 Ed25519 签名密钥与 Fernet 加密密钥（测试用途）
* ``serve``    启动本地 HTTP API
* ``policy``   publish / get / list
* ``export``   对 JSON 文件执行导出，写出导出包
* ``verify``   核验导出包（可选提供原始数据做全量重算比对）

所有输出为 UTF-8 JSON；退出码：成功 0，业务错误 2，核验失败 3。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import __version__, crypto
from .errors import MdeError, VerificationError
from .policy import FilePolicyStore, InMemoryPolicyStore
from .service import ExportService, load_bundle_file, load_data_file


def _print(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _read_json_arg(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------- keygen

def cmd_keygen(args: argparse.Namespace) -> int:
    import os
    os.makedirs(args.dir, exist_ok=True)
    priv = crypto.generate_signing_key()
    priv_path = os.path.join(args.dir, "signing_key.pem")
    pub_path = os.path.join(args.dir, "verifying_key.pem")
    crypto.save_private_key(priv, priv_path)
    crypto.save_public_key(priv.public_key(), pub_path)
    fernet_path = None
    if args.with_fernet:
        fernet_path = os.path.join(args.dir, "fernet.key")
        crypto.save_key_file(crypto.generate_fernet_key(), fernet_path)
    _print({
        "ok": True,
        "note": "LOCAL TEST KEYS ONLY. Do not use for production accounts.",
        "signing_private_key": priv_path,
        "verifying_public_key": pub_path,
        "verifying_key_hex": crypto.public_key_hex(priv),
        "fernet_key": fernet_path,
    })
    return 0


# --------------------------------------------------------------------- policy

def _policy_store_from_args(args):
    return FilePolicyStore(args.store_dir) if args.store_dir else InMemoryPolicyStore()


def cmd_policy_publish(args: argparse.Namespace) -> int:
    store = _policy_store_from_args(args)
    doc = _read_json_arg(args.file)
    if not isinstance(doc, dict):
        raise MdeError("policy file must be a JSON object")
    policy = store.publish(
        args.id,
        doc.get("rules", []),
        aliases=doc.get("aliases", []),
        default_action=doc.get("default_action", "deny"),
        description=doc.get("description", ""),
        expected_version=args.expected_version,
    )
    _print(policy.to_dict())
    return 0


def cmd_policy_get(args: argparse.Namespace) -> int:
    store = _policy_store_from_args(args)
    policy = store.get(args.id, args.version)
    _print(policy.to_dict())
    return 0


def cmd_policy_list(args: argparse.Namespace) -> int:
    store = _policy_store_from_args(args)
    _print({pid: store.list_versions(pid) for pid in store.list_policies()})
    return 0


# --------------------------------------------------------------------- export

def _build_service(args: argparse.Namespace) -> ExportService:
    store = FilePolicyStore(args.store_dir) if args.store_dir else InMemoryPolicyStore()
    priv = crypto.load_private_key(args.signing_key) if args.signing_key else None
    return ExportService(store, signing_private_key=priv,
                         bundle_dir=args.bundle_dir)


def cmd_export(args: argparse.Namespace) -> int:
    service = _build_service(args)
    data = load_data_file(args.data)
    enc_key = None
    if args.encrypt:
        if args.encryption_key:
            enc_key = crypto.load_key_file(args.encryption_key)
        else:
            enc_key = crypto.generate_fernet_key()
    bundle = service.export(
        data, args.policy_id, args.purpose,
        version=args.version, encryption_key=enc_key,
        save=bool(args.bundle_dir),
    )
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(json.dumps(bundle, ensure_ascii=False, indent=2))
            f.write("\n")
    else:
        _print(bundle)
    if args.encrypt and not args.encryption_key:
        # 一次性把新密钥打印到 stderr，避免与 stdout 的包内容混在一起。
        print(json.dumps({
            "encryption_key": enc_key.decode("ascii"),
            "warning": "SAVE THIS KEY OUT-OF-BAND. It is shown only once.",
        }, ensure_ascii=False), file=sys.stderr)
    return 0


# --------------------------------------------------------------------- verify

def cmd_verify(args: argparse.Namespace) -> int:
    service = _build_service(args)
    bundle = load_bundle_file(args.bundle)
    key = crypto.load_key_file(args.encryption_key) if args.encryption_key else None
    source = load_data_file(args.source_data) if args.source_data else None
    try:
        report = service.verify_bundle(
            bundle, encryption_key=key, source_data=source)
    except VerificationError as exc:
        _print({"ok": False, "error": str(exc)})
        return 3
    _print(report)
    return 0


# ---------------------------------------------------------------------- serve

def cmd_serve(args: argparse.Namespace) -> int:
    from .api import build_server
    store = FilePolicyStore(args.store_dir) if args.store_dir else InMemoryPolicyStore()
    priv = crypto.load_private_key(args.signing_key) if args.signing_key else None
    service = ExportService(store, signing_private_key=priv,
                            bundle_dir=args.bundle_dir)
    server = build_server(store, service, host=args.host, port=args.port)
    print(json.dumps({
        "listening": f"http://{args.host}:{args.port}",
        "store_dir": args.store_dir or "(in-memory; policies reset on restart)",
        "bundle_dir": args.bundle_dir or "(bundles not persisted by API export)",
        "verifying_key_hex": service.verifying_key_hex,
        "ephemeral_signing_key": service.key_is_ephemeral,
    }, ensure_ascii=False), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", flush=True)
    finally:
        server.server_close()
    return 0


# ----------------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mde",
        description="Minimal-disclosure record export (field-level policy "
                    "engine; not anonymization).")
    p.add_argument("--version", action="version", version=f"mde {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    common_store = argparse.ArgumentParser(add_help=False)
    common_store.add_argument("--store-dir",
                              help="directory for persisted policy JSON files")

    sp = sub.add_parser("keygen", help="generate local test keys")
    sp.add_argument("--dir", default="keys", help="output directory")
    sp.add_argument("--with-fernet", action="store_true",
                    help="also generate a Fernet encryption key")
    sp.set_defaults(func=cmd_keygen)

    spp = sub.add_parser("policy", help="manage policies")
    psub = spp.add_subparsers(dest="policy_cmd", required=True)

    pp = psub.add_parser("publish", parents=[common_store],
                         help="publish a new immutable version")
    pp.add_argument("--id", required=True)
    pp.add_argument("--file", required=True, help="policy definition JSON")
    pp.add_argument("--expected-version", type=int, default=None,
                    help="optimistic concurrency: require current version=N")
    pp.set_defaults(func=cmd_policy_publish)

    pg = psub.add_parser("get", parents=[common_store])
    pg.add_argument("--id", required=True)
    pg.add_argument("--version", type=int)
    pg.set_defaults(func=cmd_policy_get)

    pl = psub.add_parser("list", parents=[common_store])
    pl.set_defaults(func=cmd_policy_list)

    se = sub.add_parser("export", parents=[common_store],
                        help="run an export task")
    se.add_argument("--data", required=True)
    se.add_argument("--policy-id", required=True)
    se.add_argument("--purpose", required=True)
    se.add_argument("--version", type=int, help="pin to specific policy version")
    se.add_argument("--signing-key", help="Ed25519 private PEM (ephemeral if unset)")
    se.add_argument("--bundle-dir", help="persist bundle under this directory")
    se.add_argument("--encrypt", action="store_true")
    se.add_argument("--encryption-key", help="Fernet key file (generated if absent)")
    se.add_argument("--out", help="write bundle JSON to this path")
    se.set_defaults(func=cmd_export)

    sv = sub.add_parser("verify", parents=[common_store],
                        help="verify an export bundle")
    sv.add_argument("--bundle", required=True)
    sv.add_argument("--signing-key", help="accepted for symmetry (not needed)")
    sv.add_argument("--encryption-key", help="Fernet key file for encrypted bundle")
    sv.add_argument("--source-data",
                    help="original input JSON; enables full re-computation check")
    sv.add_argument("--bundle-dir")
    sv.set_defaults(func=cmd_verify)

    ss = sub.add_parser("serve", parents=[common_store],
                        help="run local HTTP API")
    ss.add_argument("--host", default="127.0.0.1")
    ss.add_argument("--port", type=int, default=8080)
    ss.add_argument("--signing-key", help="Ed25519 private PEM")
    ss.add_argument("--bundle-dir")
    ss.set_defaults(func=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except MdeError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
