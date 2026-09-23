"""生成一个 Ed25519 操作者密钥对 (用于离线签名事件)。

用法:
    python scripts/generate_keys.py

输出 PEM 私钥到 data/keys/operator_ed25519.pem (权限 600),
并打印公钥 hex 及导出环境变量命令。服务端报告签名密钥在服务
首次启动时自动生成 (data/keys/server_ed25519.pem)。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.crypto import generate_private_key, public_hex, save_private_key  # noqa: E402


def main() -> None:
    keys_dir = settings.keys_dir
    path = keys_dir / "operator_ed25519.pem"
    if path.exists():
        print(f"operator key already exists at {path}; refusing to overwrite")
        return
    key = generate_private_key()
    save_private_key(key, path)
    pub = public_hex(key)
    print(f"private key written (mode 600): {path}")
    print(f"operator public key (hex):    {pub}")
    print()
    print("启动服务前导出:")
    print(f'  export LIQREPLAY_OPERATOR_KEYS="{pub}"')


if __name__ == "__main__":
    main()
