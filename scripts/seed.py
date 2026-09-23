"""把 examples/events.py 中的示例事件用操作者 Ed25519 私钥签名后提交。

前置:
  1) 服务已启动 (uvicorn app.main:app)
  2) 已运行 scripts/generate_keys.py, 并以 LIQREPLAY_OPERATOR_KEYS 启动服务

用法:
  python scripts/seed.py [--base-url http://127.0.0.1:8000]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from app.config import settings  # noqa: E402
from app.crypto import load_private_key, public_hex, sign_payload  # noqa: E402
from examples.events import example_events  # noqa: E402


def signed_events():
    key_path = settings.keys_dir / "operator_ed25519.pem"
    if not key_path.exists():
        print(f"missing operator key: {key_path}")
        print("run: python scripts/generate_keys.py")
        print("then start the server with:")
        print('  LIQREPLAY_OPERATOR_KEYS="<printed pubkey>" uvicorn app.main:app')
        sys.exit(2)
    key = load_private_key(key_path)
    signer = public_hex(key)
    out = []
    for ev in example_events():
        body = {k: ev[k] for k in ("event_id", "ts", "seq", "action", "payload")}
        out.append({**body, "signer": signer, "signature": sign_payload(key, body)})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = ap.parse_args()

    events = signed_events()
    with httpx.Client(trust_env=False) as client:
        resp = client.post(
            f"{args.base_url}/api/v1/events", json=events, timeout=30
        )
    print(f"POST /api/v1/events -> {resp.status_code}")
    print(json.dumps(resp.json(), indent=2, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
