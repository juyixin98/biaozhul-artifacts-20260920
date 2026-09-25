#!/usr/bin/env python3
"""Generate the test-key certificate chains under fixtures/ on this machine.

Run once (or whenever fixtures must be regenerated):

    python scripts/generate_fixtures.py

All keys are generated locally with cryptography; nothing is downloaded and
no real-world CA keys are involved. Each scenario directory gets:

    root.pem / intermediate*.pem / leaf.pem     PEM certificates
    leaf.key.pem                                 leaf PRIVATE key (test only)
    request.json                                 example /verify request body
    scenario.txt                                 human description
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cryptography.hazmat.primitives import serialization  # noqa: E402

from certverifier.fixtures import build_all_scenarios  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"


def pem(cert) -> str:
    return cert.public_bytes(serialization.Encoding.PEM).decode("ascii")


def key_pem(key) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")


def main() -> int:
    FIXTURES.mkdir(exist_ok=True)
    scenarios = build_all_scenarios()
    index = {}
    for name, sc in scenarios.items():
        directory = FIXTURES / name
        directory.mkdir(parents=True, exist_ok=True)

        (directory / "root.pem").write_text(
            "".join(pem(c) for c in sc.anchors), encoding="ascii"
        )
        # one combined intermediates file, plus individual files
        (directory / "intermediates.pem").write_text(
            "".join(pem(c) for c in sc.all_intermediates), encoding="ascii"
        )
        for i, cert in enumerate(sc.all_intermediates):
            (directory / f"intermediate_{i}.pem").write_text(pem(cert), encoding="ascii")
        (directory / "leaf.pem").write_text(pem(sc.leaf), encoding="ascii")
        (directory / "leaf.key.pem").write_text(key_pem(sc.leaf_key), encoding="ascii")

        request = {
            "leaf_certificate": pem(sc.leaf),
            "intermediates": "".join(pem(c) for c in sc.all_intermediates),
            "trust_anchors": "".join(pem(c) for c in sc.anchors),
            "verification_time": sc.verification_time.isoformat().replace("+00:00", "Z"),
            "purpose": sc.purpose,
            "hostname": sc.hostname,
        }
        (directory / "request.json").write_text(
            json.dumps(request, indent=2), encoding="ascii"
        )
        (directory / "scenario.txt").write_text(
            f"name: {sc.name}\n"
            f"description: {sc.description}\n"
            f"verification_time: {request['verification_time']}\n"
            f"hostname: {sc.hostname}\n"
            f"expected_valid: {sc.expect_valid}\n"
            f"expected_codes: {sorted(sc.expected_codes)}\n",
            encoding="utf-8",
        )
        index[name] = {
            "description": sc.description,
            "expected_valid": sc.expect_valid,
            "expected_codes": sorted(sc.expected_codes),
        }

    (FIXTURES / "index.json").write_text(
        json.dumps(index, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(f"wrote {len(scenarios)} scenarios under {FIXTURES}")
    for name in sorted(scenarios):
        print(f"  - {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
