"""SAM 命令行入口: keygen / sign / verify / run。

退出码约定:
  0  成功
  2  验证失败 (verify 子命令: 报告 ok=false, 但流程本身正常)
  3  用法/环境错误 (文件不存在、私钥解析失败等 SAMError)
  4  执行制品时以非零码退出时, CLI 透传该退出码
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from cryptography.hazmat.primitives.serialization import load_pem_public_key

from . import canonical
from .errors import SAMError
from .keys import TrustStore, load_private_key, public_key_id
from .runner import run as run_artifact
from .sign import sign_artifact, generate_keypair_files
from .verify import verify as verify_report

EXIT_OK = 0
EXIT_VERIFY_FAILED = 2
EXIT_USAGE = 3


def _cmd_keygen(args: argparse.Namespace) -> int:
    private_path = Path(args.private_key)
    public_path = Path(args.public_key)
    if private_path.exists() and not args.force:
        raise SAMError(f"私钥已存在, 拒绝覆盖 (--force 可强制): {private_path}")
    public_pem = generate_keypair_files(private_path)
    public_path.parent.mkdir(parents=True, exist_ok=True)
    if public_path.exists() and not args.force:
        raise SAMError(f"公钥已存在, 拒绝覆盖 (--force 可强制): {public_path}")
    public_path.write_text(public_pem, encoding="utf-8")

    kid = public_key_id(load_pem_public_key(public_pem.encode("ascii")))
    print(f"已生成 Ed25519 测试密钥")
    print(f"  私钥(0600): {private_path}")
    print(f"  公钥      : {public_path}")
    print(f"  key_id    : {kid}")
    return EXIT_OK


def _cmd_sign(args: argparse.Namespace) -> int:
    root = Path(args.artifact_root).resolve()
    if not root.is_dir():
        raise SAMError(f"制品根不是目录: {root}")
    private_key = load_private_key(args.private_key)
    envelope = sign_artifact(root, private_key, entrypoint=args.entrypoint)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(canonical.pretty_json(envelope))
    n = len(envelope["manifest"]["files"])
    print(f"已对 {n} 个文件签名 -> {out}")
    print(f"  key_id: {envelope['manifest']['key_id']}")
    print("注意: 封套应保存在制品根之外 (严格验证会把清单外文件视为污染)")
    return EXIT_OK


def _cmd_verify(args: argparse.Namespace) -> int:
    store = TrustStore.load(args.trust_dir)
    envelope_bytes = Path(args.envelope).read_bytes()
    report = verify_report(
        artifact_root=args.artifact_root,
        envelope_bytes=envelope_bytes,
        trust_store=store,
        strict_extra=not args.allow_extra,
    )
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        if report.ok:
            print(f"验证通过: {report.file_count} 个文件, key_id={report.key_id}")
            if report.entrypoint:
                print(f"  entrypoint: {report.entrypoint}")
        else:
            print(f"验证失败: 共 {len(report.problems)} 个问题", file=sys.stderr)
            for p in report.problems:
                loc = f" [{p.path}]" if p.path else ""
                print(f"  - {p.code}{loc}: {p.message}", file=sys.stderr)
    return EXIT_OK if report.ok else EXIT_VERIFY_FAILED


def _cmd_run(args, extra: list[str]) -> int:
    interpreter = ["python3"] if args.python else None
    code = run_artifact(
        artifact_root=args.artifact_root,
        envelope_path=args.envelope,
        trust_dir=args.trust_dir,
        entry_args=extra,
        interpreter=interpreter,
    )
    return code  # 透传制品退出码


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sam",
        description="签名制品清单 (Signed Artifact Manifest) —— 离线签名/验证/执行",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("keygen", help="本地生成 Ed25519 测试密钥对")
    p.add_argument("--private-key", required=True)
    p.add_argument("--public-key", required=True)
    p.add_argument("--force", action="store_true", help="覆盖已有文件")
    p.set_defaults(func=_cmd_keygen)

    p = sub.add_parser("sign", help="对制品目录签名, 输出清单封套 JSON")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--private-key", required=True)
    p.add_argument("--output", required=True, help="封套输出路径 (应在制品根之外)")
    p.add_argument("--entrypoint", help="可选: 制品内相对可执行文件")
    p.set_defaults(func=_cmd_sign)

    p = sub.add_parser("verify", help="用本地信任库验证制品与封套")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--envelope", required=True)
    p.add_argument("--trust-dir", required=True, help="可信公钥 *.pem 所在目录或单个 pem")
    p.add_argument("--allow-extra", action="store_true", help="不把清单外文件视为失败")
    p.add_argument("--json", action="store_true", help="输出机器可读 JSON 报告")
    p.set_defaults(func=_cmd_verify)

    p = sub.add_parser(
        "run",
        help="先验证, 通过后执行 entrypoint (验证失败绝不执行)",
    )
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--envelope", required=True)
    p.add_argument("--trust-dir", required=True)
    p.add_argument("--python", action="store_true", help="以 python3 解释器运行 entrypoint")
    p.set_defaults(func=_cmd_run)

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # 让 "run" 子命令可以接收透传给制品的参数: sam run ... -- arg1 arg2
    passthrough: list[str] = []
    if "--" in argv:
        idx = argv.index("--")
        passthrough = argv[idx + 1 :]
        argv = argv[:idx]

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            return _cmd_run(args, passthrough)
        return args.func(args)
    except SAMError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except FileNotFoundError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
