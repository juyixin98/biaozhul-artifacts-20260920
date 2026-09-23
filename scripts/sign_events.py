"""对事件做 Ed25519 签名（真实签名，非模拟）。

用法：
  python scripts/sign_events.py <未签名 JSONL> <输出 JSONL>

输入每行：{"event_id","ts","type","payload"}
输出每行：同上 + "sig"（hex）。
默认从 keys/feeder_private.hex 读取私钥，可用 FEEDER_PRIVATE_KEY_HEX 覆盖。
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nacl.signing import SigningKey  # noqa: E402

from app.crypto import event_signing_bytes  # noqa: E402


def load_key() -> SigningKey:
    env = os.environ.get("FEEDER_PRIVATE_KEY_HEX", "")
    if env:
        return SigningKey(bytes.fromhex(env))
    path = os.path.join(os.path.dirname(__file__), "..", "keys", "feeder_private.hex")
    return SigningKey(bytes.fromhex(open(path, "r", encoding="ascii").read().strip()))


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    sk = load_key()
    n = 0
    with open(sys.argv[1], "r", encoding="utf-8") as fin, open(
        sys.argv[2], "w", encoding="utf-8"
    ) as fout:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            ev = json.loads(line)
            ev["sig"] = sk.sign(event_signing_bytes(ev)).signature.hex()
            fout.write(json.dumps(ev, ensure_ascii=False) + "\n")
            n += 1
    print(f"signed {n} events -> {sys.argv[2]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
