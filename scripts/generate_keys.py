"""生成演示用 Ed25519 密钥对（真实密码学随机数）。

feeder  ：事件提交方私钥（sign_events.py 使用），对应公钥由服务验签。
server  ：报告签名私钥（服务启动加载），公钥可用 verify_report.py 验证报告。

⚠️ 演示密钥仅供本地验收，生产环境请离线自行生成并通过环境变量注入。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.crypto import generate_keypair


def main() -> int:
    kd = os.path.join(os.path.dirname(__file__), "..", "keys")
    kd = os.path.abspath(kd)
    os.makedirs(kd, exist_ok=True)

    pairs = {}
    for name in ("feeder", "server"):
        target_priv = os.path.join(kd, f"{name}_private.hex")
        target_pub = os.path.join(kd, f"{name}_public.hex")
        if os.path.exists(target_priv):
            print(f"[skip] {target_priv} 已存在，未覆盖", file=sys.stderr)
            continue
        kp = generate_keypair()
        with open(target_priv, "w", encoding="ascii") as f:
            f.write(kp.private_hex + "\n")
        with open(target_pub, "w", encoding="ascii") as f:
            f.write(kp.public_hex + "\n")
        os.chmod(target_priv, 0o600)
        pairs[name] = kp
        print(f"[ok] 写入 {target_priv} / {target_pub}")

    for name, kp in pairs.items():
        print(f"{name} public key: {kp.public_hex}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
