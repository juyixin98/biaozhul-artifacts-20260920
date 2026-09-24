"""Command-line entry point: audit an archive file and print JSON.

Usage:
    python -m app.lockgate.cli path/to/bundle.tar.gz [--os linux --cpu x64]
"""
from __future__ import annotations

import argparse
import json
import sys

from .archive import UnsafeArchiveError, parse_archive
from .audit import audit
from .platforms import Target


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="lockgate")
    ap.add_argument("archive", help="path to .zip/.tar/.tar.gz project bundle")
    ap.add_argument("--os", dest="os_name", default=None)
    ap.add_argument("--cpu", default=None)
    ap.add_argument("--libc", default=None)
    ap.add_argument("--no-dev", action="store_true")
    ap.add_argument("--registry", action="append", default=None,
                    help="allowed registry host (repeatable)")
    ap.add_argument("--package-path", default=None)
    ap.add_argument("--lock-path", default=None)
    args = ap.parse_args(argv)

    with open(args.archive, "rb") as fh:
        data = fh.read()
    try:
        arch = parse_archive(data)
    except UnsafeArchiveError as exc:
        print(json.dumps({"ok": False, "error": f"unsafe archive: {exc}"}),
              file=sys.stderr)
        return 2

    target = None
    if args.os_name or args.cpu:
        if not (args.os_name and args.cpu):
            ap.error("--os and --cpu must be given together")
        target = Target(os=args.os_name, cpu=args.cpu, libc=args.libc)

    result = audit(
        arch,
        target=target,
        include_dev=not args.no_dev,
        allowed_registries=args.registry,
        package_path=args.package_path,
        lock_path=args.lock_path,
    )
    json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
