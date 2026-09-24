#!/usr/bin/env python3
"""生成 README 中使用的全部 HTTP 请求样例到 examples/。

前提：先运行 scripts/generate_keys.py 生成 test-keys/。
用法：
    python scripts/make_examples.py
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.models import RotateRequest  # noqa: E402
from scripts.sighelp import (  # noqa: E402
    approval_block,
    b64e,
    dump_json,
    load_private,
    load_public,
    make_envelope,
    make_root_body,
    sha256_hex,
)

KEYS = ROOT / "test-keys"
OUT = ROOT / "examples"


def main() -> None:
    OUT.mkdir(exist_ok=True)

    # ---- 初始信任根：3 个根密钥（阈值 2），3 个制品签名密钥（阈值 2） ----
    old_root_pubs = [load_public(KEYS / f"root{i}.pub.pem") for i in (1, 2, 3)]
    signer_privs = [load_private(KEYS / f"signer{i}.priv.pem") for i in (1, 2, 3)]
    signer_pubs = [p.public_key() for p in signer_privs]

    root_v1 = make_root_body(1, 2, 2, old_root_pubs, signer_pubs)
    dump_json(root_v1.model_dump(), OUT / "01_bootstrap.json")

    # ---- 合法制品：report 类型，版本 1.0.0，由 signer1+signer2 签名 ----
    content = b'{"report": "quarterly-sales", "figures": [1, 2, 3]}'
    (OUT / "artifact_content.bin").write_bytes(content)

    envelope = make_envelope(content, "report", "1.0.0", signer_privs[:2])
    valid_req = {
        "envelope": envelope.model_dump(),
        "content_base64": b64e(content),
    }
    dump_json(valid_req, OUT / "02_verify_valid.json")
    dump_json(valid_req, OUT / "03_register.json")

    # 正文篡改：digest 不变，content 被改
    tampered = dict(valid_req)
    tampered["content_base64"] = b64e(
        b'{"report": "quarterly-sales", "figures": [1, 9, 3]}'
    )
    dump_json(tampered, OUT / "04_verify_content_tampered.json")

    # 跨类型复用：同一签名块（为 report 而签）声称自己是 container-image
    cross = {
        "envelope": {
            **envelope.model_dump(),
            "artifact_type": "container-image",
        },
        "content_base64": b64e(content),
    }
    dump_json(cross, OUT / "05_verify_cross_type.json")

    # 版本回退/不一致复用：签名块绑定 1.0.0，却声称 0.9.0
    ver = {
        "envelope": {
            **envelope.model_dump(),
            "version": "0.9.0",
        },
        "content_base64": b64e(content),
    }
    dump_json(ver, OUT / "06_verify_version_mismatch.json")

    # 重复签名：signer1 的同一块放两遍（凑数无效）
    dup_blocks = [envelope.signatures[0], envelope.signatures[0]]
    dup = {
        "envelope": {
            **envelope.model_dump(),
            "signatures": [b.model_dump() for b in dup_blocks],
        },
        "content_base64": b64e(content),
    }
    dump_json(dup, OUT / "07_verify_duplicate_signature.json")

    # 未授权签名者：用根密钥签制品（角色分离，应被拒绝）
    root_priv = load_private(KEYS / "root1.priv.pem")
    env_root_signed = make_envelope(
        content, "report", "1.0.0", [root_priv, signer_privs[0]]
    )
    dump_json(
        {"envelope": env_root_signed.model_dump(), "content_base64": b64e(content)},
        OUT / "08_verify_unauthorized_signer.json",
    )

    # ---- 信任根轮换：新根 v2，新根密钥 + 新签名密钥，旧根 2/3 批准 ----
    new_root_pubs = [
        load_public(KEYS / f"root{i}_v2.pub.pem") for i in (1, 2, 3)
    ]
    new_signer_pubs = [
        load_public(KEYS / f"signer{i}_v2.pub.pem") for i in (1, 2)
    ]
    root_v2 = make_root_body(2, 2, 2, new_root_pubs, new_signer_pubs)
    old_root_privs = [load_private(KEYS / f"root{i}.priv.pem") for i in (1, 2, 3)]
    approvals = [approval_block(p, root_v2) for p in old_root_privs[:2]]
    rotate_req = RotateRequest(new_root=root_v2, approvals=approvals)
    dump_json(rotate_req.model_dump(), OUT / "09_rotate_v2.json")

    # 回退尝试：在 v2 之上提交 version=1（应被拒绝）
    rotate_rollback = RotateRequest(
        new_root=root_v1,
        approvals=[approval_block(p, root_v1) for p in old_root_privs[:2]],
    )
    dump_json(
        rotate_rollback.model_dump(), OUT / "10_rotate_rollback_rejected.json"
    )

    # 批准不足：只给 1 个旧根批准（阈值 2）
    rotate_quorum = RotateRequest(
        new_root=root_v2,
        approvals=[approval_block(old_root_privs[0], root_v2)],
    )
    dump_json(
        rotate_quorum.model_dump(), OUT / "11_rotate_insufficient_approvals.json"
    )

    # 轮换后，旧签名密钥即失效：v1 的信封在 v2 根下验证
    dump_json(valid_req, OUT / "12_verify_after_rotation_old_signer.json")

    print(f"已生成 {len(list(OUT.glob('*.json')))} 个请求样例到 {OUT}/")


if __name__ == "__main__":
    main()
