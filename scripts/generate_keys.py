#!/usr/bin/env python3
"""生成全部测试密钥（Ed25519，仅用于本地测试，非生产凭据）。

用法：
    python scripts/generate_keys.py [输出目录，默认 test-keys]

产物（PEM 文本，可直接查看；test-keys/ 已在 .gitignore 中忽略）：
    root1/root2/root3.priv.pem + .pub.pem        旧信任根（3 个根角色密钥，阈值 2）
    signer1/signer2/signer3.priv.pem + .pub.pem  制品签名者（阈值 2）
    root1_v2/root2_v2...                          轮换后的新根密钥
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.sighelp import generate_party  # noqa: E402

OLD_ROOT_KEYS = ["root1", "root2", "root3"]
SIGNER_KEYS = ["signer1", "signer2", "signer3"]
NEW_ROOT_KEYS = ["root1_v2", "root2_v2", "root3_v2"]
NEW_SIGNER_KEYS = ["signer1_v2", "signer2_v2"]


def main(out_dir: str = "test-keys") -> None:
    d = Path(out_dir)
    for name in OLD_ROOT_KEYS + SIGNER_KEYS + NEW_ROOT_KEYS + NEW_SIGNER_KEYS:
        generate_party(d, name)
    print(f"已在 {d}/ 生成 "
          f"{len(OLD_ROOT_KEYS) + len(SIGNER_KEYS) + len(NEW_ROOT_KEYS) + len(NEW_SIGNER_KEYS)} "
          "对 Ed25519 测试密钥（.priv.pem / .pub.pem）。")
    print("警告：这些是本地测试密钥，严禁用于生产。")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "test-keys")
