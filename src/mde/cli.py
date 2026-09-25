"""mde 命令行：离线导出、复验与本地服务。

用法::

    python -m mde.cli export  --records r.json --purpose analytics --policy-fingerprint FP [-o pkg.json]
    python -m mde.cli publish --policy p.json
    python -m mde.cli verify  --package pkg.json [--records r.json] [--local-keys]
    python -m mde.cli serve   [--host 127.0.0.1] [--port 8390] [--data-dir .mde-data]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .exporter import ExportService, save_package, verify_package, utc_now_iso
from .keys import load_or_create
from .policy import PolicyError, PolicyStore


def _read_json(path: str):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _services(data_dir: Path):
    key_dir = data_dir / "keys"
    keys, created = load_or_create(key_dir)
    if created:
        print(f"[mde] 已生成本地测试密钥: {key_dir / 'test-keys.json'} (0600)", file=sys.stderr)
    store = PolicyStore(data_dir / "policies")
    return keys, store, ExportService(store, keys)


def cmd_publish(args) -> int:
    keys, store, _ = _services(Path(args.data_dir))
    doc = _read_json(args.policy)
    try:
        stored = store.publish(doc, utc_now_iso())
    except PolicyError as e:
        print(f"策略校验失败: {e}", file=sys.stderr)
        return 2
    print(json.dumps({
        "policy_fingerprint": stored.policy.policy_fingerprint,
        "policy_id": doc["policy_id"],
        "revision": doc["revision"],
        "created_at": stored.created_at,
    }, indent=2, ensure_ascii=False))
    return 0


def cmd_export(args) -> int:
    keys, store, service = _services(Path(args.data_dir))
    records = _read_json(args.records)
    if not isinstance(records, list):
        print("records 文件必须是 JSON 数组", file=sys.stderr)
        return 2
    try:
        package = service.export(
            records=records, purpose=args.purpose,
            policy_fingerprint=args.policy_fingerprint,
        )
    except PolicyError as e:
        print(f"导出失败: {e}", file=sys.stderr)
        return 2
    text = json.dumps(package, indent=2, ensure_ascii=False)
    if args.output:
        save_package(package, Path(args.output))
        print(f"已写出导出包: {args.output}", file=sys.stderr)
    else:
        print(text)
    return 0


def cmd_verify(args) -> int:
    data_dir = Path(args.data_dir)
    package = _read_json(args.package)
    records = _read_json(args.records) if args.records else None
    keys = None
    if args.local_keys:
        keys, _, _ = _services(data_dir)
    report = verify_package(package, records=records, keys=keys)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["overall_passed"] else 1


def cmd_serve(args) -> int:
    from .api import serve

    print(f"[mde] 本地服务监听 http://{args.host}:{args.port} （仅回环，勿暴露网络）",
          file=sys.stderr)
    serve(args.host, args.port, Path(args.data_dir))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mde", description="最小披露记录导出（纯后端）")
    p.add_argument("--data-dir", default=".mde-data", help="本地数据/密钥目录")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("publish", help="发布不可变策略")
    sp.add_argument("--policy", required=True)
    sp.set_defaults(func=cmd_publish)

    se = sub.add_parser("export", help="按用途与策略指纹导出")
    se.add_argument("--records", required=True)
    se.add_argument("--purpose", required=True)
    se.add_argument("--policy-fingerprint", required=True)
    se.add_argument("-o", "--output")
    se.set_defaults(func=cmd_export)

    sv = sub.add_parser("verify", help="复验导出包")
    sv.add_argument("--package", required=True)
    sv.add_argument("--records", help="提供原始记录以重算输入摘要")
    sv.add_argument("--local-keys", action="store_true",
                    help="使用本地 transform_secret 做端到端重放")
    sv.set_defaults(func=cmd_verify)

    ss = sub.add_parser("serve", help="启动本地 HTTP 服务")
    ss.add_argument("--host", default="127.0.0.1")
    ss.add_argument("--port", type=int, default=8390)
    ss.set_defaults(func=cmd_serve)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
