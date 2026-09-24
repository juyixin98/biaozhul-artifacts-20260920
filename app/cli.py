"""Command-line entry point: verify a bundle on disk and print the report.

Usage::

    python -m app.cli verify examples/happy_bundle.tgz --require-vendored
    python -m app.cli verify project.zip --os darwin --cpu arm64

Exit code is 0 when the gate passes, 1 when findings fail the gate and 2 for
bad invocation (missing/unreadable bundle). The full JSON report is always
printed on stdout so it can be archived as CI evidence.
"""

from __future__ import annotations

import argparse
import json
import sys

from .extract import BundleError, extract_bundle
from .verifier import VALID_CPU, VALID_LIBC, VALID_OS, VerifierInputError, verify_bundle


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lock-gate",
        description="Offline npm dependency-lock consistency gate",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    verify = sub.add_parser("verify", help="verify a .tgz/.tar.gz/.zip bundle")
    verify.add_argument("bundle", help="path to the project archive")
    verify.add_argument("--os", default="linux", choices=sorted(VALID_OS))
    verify.add_argument("--cpu", default="x64", choices=sorted(VALID_CPU))
    verify.add_argument(
        "--libc", default="glibc", choices=sorted(VALID_LIBC), nargs="?",
        const=None,
    )
    verify.add_argument(
        "--production", action="store_true", help="exclude devDependencies"
    )
    verify.add_argument(
        "--require-vendored",
        action="store_true",
        help="missing vendor tarballs become hard errors",
    )
    verify.add_argument(
        "--pretty", action="store_true", help="indent the JSON report"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command != "verify":  # pragma: no cover - argparse enforces this
        return 2
    try:
        with open(args.bundle, "rb") as handle:
            blob = handle.read()
        bundle = extract_bundle(blob)
    except OSError as exc:
        print(json.dumps({"status": "fail", "error": f"cannot read bundle: {exc}"}))
        return 2
    except BundleError as exc:
        print(
            json.dumps(
                {"status": "fail", "error": f"rejected bundle: {exc}"},
                ensure_ascii=False,
            )
        )
        return 2

    try:
        report = verify_bundle(
            bundle.files,
            os_name=args.os,
            cpu=args.cpu,
            libc=args.libc,
            production=args.production,
            require_vendored_tarballs=args.require_vendored,
        )
    except VerifierInputError as exc:
        print(json.dumps({"status": "fail", "error": str(exc)}, ensure_ascii=False))
        return 2

    print(json.dumps(report, indent=2 if args.pretty else None, ensure_ascii=False))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
