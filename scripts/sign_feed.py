"""用 Ed25519 私钥对漏洞馈送 payload 做分离签名，产出信封 JSON。

用法:
    python -m scripts.sign_feed fixtures/vuln-feed.payload.json \\
        fixtures/keys/test_feed_private.pem \\
        fixtures/vuln-feed.json

签名规则必须与 sbom_risk.feed.canonical_payload_bytes 一致。
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sbom_risk.feed import FeedPayload, canonical_payload_bytes  # noqa: E402

KID = "test-fixture-key-2026"


def sign(payload_path: Path, private_key_path: Path, envelope_path: Path) -> None:
    # 先规范化为模型再签名，保证签名字节与服务端 FeedPayload.model_dump() 完全一致
    payload = FeedPayload.model_validate(
        json.loads(payload_path.read_text(encoding="utf-8"))
    )
    private_key = serialization.load_pem_private_key(
        private_key_path.read_bytes(), password=None
    )
    signature = private_key.sign(canonical_payload_bytes(payload))
    envelope = {
        "alg": "ed25519",
        "kid": KID,
        "signature_b64": base64.b64encode(signature).decode("ascii"),
        "payload": payload.model_dump(),
    }
    envelope_path.write_text(
        json.dumps(envelope, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"已签名并写入: {envelope_path} ({len(signature)} 字节签名)")


def main(argv: list[str]) -> None:
    if len(argv) != 4:
        print(__doc__)
        raise SystemExit(2)
    sign(Path(argv[1]), Path(argv[2]), Path(argv[3]))


if __name__ == "__main__":
    main(sys.argv)
