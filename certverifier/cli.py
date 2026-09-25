"""Command line interface for offline chain verification.

Examples:
    python -m certverifier.cli \\
        --leaf fixtures/good/leaf.pem \\
        --intermediates fixtures/good/intermediate.pem \\
        --trust-anchor fixtures/good/root.pem \\
        --verification-time 2025-06-01T12:00:00Z \\
        --purpose serverAuth \\
        --hostname example.test
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .models import Purpose, VerificationOptions
from .pemutils import parse_pem_certificates
from .verify import verify_chain


def _read_pem(path: str) -> list:
    return parse_pem_certificates(Path(path).read_text(encoding="ascii"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify an X.509 chain offline (no CRL/OCSP, no AIA fetch)."
    )
    parser.add_argument("--leaf", required=True, help="PEM leaf certificate")
    parser.add_argument(
        "--intermediates", action="append", default=[],
        help="PEM intermediate file(s); repeatable or a multi-cert PEM",
    )
    parser.add_argument(
        "--trust-anchor", action="append", required=True, dest="trust_anchors",
        help="PEM trust anchor (root) file(s); repeatable",
    )
    parser.add_argument(
        "--verification-time", required=True,
        help="ISO 8601 verification moment, e.g. 2025-06-01T12:00:00Z "
        "(always explicit; never silently 'now')",
    )
    parser.add_argument(
        "--purpose", default=Purpose.SERVER_AUTH.value,
        choices=[p.value for p in Purpose],
    )
    parser.add_argument("--hostname", default=None, help="DNS name or IP to match the SAN")
    parser.add_argument("--check-anchor-validity", action="store_true")
    parser.add_argument("--allow-cn-hostname", action="store_true")
    parser.add_argument("--allow-weak-signatures", action="store_true")
    parser.add_argument("--pretty", action="store_true", default=True)
    args = parser.parse_args(argv)

    try:
        leaf_certs = _read_pem(args.leaf)
        if len(leaf_certs) != 1:
            raise ValueError("--leaf must contain exactly one certificate")
        intermediates = []
        for path in args.intermediates:
            intermediates.extend(_read_pem(path))
        anchors = []
        for path in args.trust_anchors:
            anchors.extend(_read_pem(path))
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2

    text = args.verification_time.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    when = datetime.fromisoformat(text)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)

    options = VerificationOptions(
        check_anchor_validity=args.check_anchor_validity,
        allow_cn_hostname=args.allow_cn_hostname,
        allow_weak_signature_algorithms=args.allow_weak_signatures,
    )

    result = verify_chain(
        leaf_certs[0], intermediates, anchors,
        verification_time=when,
        purpose=Purpose(args.purpose),
        hostname=args.hostname,
        options=options,
    )
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    return 0 if result.valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
