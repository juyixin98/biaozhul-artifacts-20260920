#!/usr/bin/env python3
"""端到端 HTTP 演示（需要服务已启动：uvicorn app.main:app --port 8000）。

脚本自动生成临时测试密钥，依次演示：
  1. 初始化信任根（threshold=2/3）
  2. 正常签名 + 验签
  3. 正文篡改被拒
  4. 跨制品类型搬用签名被拒
  5. 重复签名 / nonce 重放被拒
  6. 版本回退被拒
  7. 旧根阈值批准的根轮换（批准不足被拒 -> 满足阈值成功）
  8. 轮换后旧签名密钥失效
  9. 无状态验签识别不受信任的签名者

用法::

    python examples/demo.py [base_url]
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

from app import crypto

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
_passed = 0
_failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global _passed, _failed
    mark = "PASS" if ok else "FAIL"
    if ok:
        _passed += 1
    else:
        _failed += 1
    print(f"  [{mark}] {name}" + (f" —— {detail}" if detail else ""))


def envelope(signer, artifact_type, version, content):
    digest = crypto.sha256_hex(content)
    nonce = crypto.generate_nonce()
    signature = crypto.sign_artifact(
        private_key_hex=signer.private_key_hex,
        digest_hex=digest,
        artifact_type=artifact_type,
        version=version,
        nonce_hex=nonce,
    )
    return {
        "artifact_type": artifact_type,
        "version": version,
        "digest": digest,
        "key_id": signer.kid,
        "nonce": nonce,
        "signature": signature,
    }


def main() -> int:
    # 演示使用全新的临时数据目录：启动服务时请设置 STATE_PATH 指向该目录。
    tmp = tempfile.mkdtemp(prefix="artifact-sign-demo-")
    print(f"提示：如服务数据非空，可重启服务并设置 STATE_PATH={tmp}/state.json\n")

    root_keys = [crypto.generate_keypair() for _ in range(3)]
    signer_a, signer_b = [crypto.generate_keypair() for _ in range(2)]
    new_root_keys = [crypto.generate_keypair() for _ in range(2)]
    new_signer = crypto.generate_keypair()

    c = httpx.Client(base_url=BASE, timeout=10)

    print("== 1. 初始化信任根 (threshold=2/3) ==")
    r = c.post(
        "/root/init",
        json={
            "root_version": 1,
            "threshold": 2,
            "threshold_public_keys": [k.public_key_hex for k in root_keys],
            "signer_public_keys": [signer_a.public_key_hex, signer_b.public_key_hex],
        },
    )
    print("   POST /root/init ->", r.status_code)
    if r.status_code == 409:
        print("   服务里已有信任根。请用空数据重启，例如：")
        print(f"   STATE_PATH={tmp}/state.json uvicorn app.main:app --port 8000")
        return 2
    check("根初始化成功", r.status_code == 201)

    print("\n== 2. 正常签名登记 + 验签 ==")
    content = b"firmware-v1-release"
    env = envelope(signer_a, "firmware", "1.0.0", content)
    r = c.post("/artifacts/sign", json=env)
    check("登记 201", r.status_code == 201, str(r.status_code))
    r = c.post(
        "/artifacts/verify",
        json={k: env[k] for k in ("artifact_type", "version", "digest", "nonce", "signature")},
    )
    body = r.json()
    check("原文验签 valid=true", r.status_code == 200 and body["valid"] is True, body["reason"])

    print("\n== 3. 正文篡改 ==")
    r = c.post(
        "/artifacts/verify",
        json={
            **{k: env[k] for k in ("artifact_type", "version", "nonce", "signature")},
            "digest": crypto.sha256_hex(b"firmware-v1-TAMPERED"),
        },
    )
    body = r.json()
    check("篡改后 valid=false", body["valid"] is False, body["reason"])

    print("\n== 4. 跨类型搬用签名 ==")
    cross = envelope(signer_a, "firmware", "1.0.0", b"same bytes")
    cross["artifact_type"] = "container-image"
    r = c.post("/artifacts/sign", json=cross)
    check("跨类型登记 400", r.status_code == 400, str(r.status_code))
    r = c.post(
        "/artifacts/verify",
        json={**cross, "public_key": signer_a.public_key_hex},
    )
    check("无状态验签 valid=false", r.json()["valid"] is False, r.json()["reason"])

    print("\n== 5. 重复签名 / nonce 重放 ==")
    r = c.post("/artifacts/sign", json=env)
    check("完全重复 409", r.status_code == 409, r.json().get("detail", ""))
    replay_digest = crypto.sha256_hex(b"other body")
    replay_sig = crypto.sign_artifact(
        private_key_hex=signer_a.private_key_hex,
        digest_hex=replay_digest,
        artifact_type="addon",
        version="1.0.0",
        nonce_hex=env["nonce"],  # 复用旧 nonce
    )
    r = c.post(
        "/artifacts/sign",
        json={
            "artifact_type": "addon",
            "version": "1.0.0",
            "digest": replay_digest,
            "key_id": signer_a.kid,
            "nonce": env["nonce"],
            "signature": replay_sig,
        },
    )
    check("nonce 重放 409", r.status_code == 409, r.json().get("detail", ""))

    print("\n== 6. 版本回退 ==")
    for ver in ["1.2.0", "2.0.0"]:
        e = envelope(signer_a, "firmware", ver, f"body-{ver}".encode())
        r = c.post("/artifacts/sign", json=e)
        check(f"登记 {ver} 201", r.status_code == 201, str(r.status_code))
    for bad in ["1.9.9", "2.0.0"]:
        e = envelope(signer_a, "firmware", bad, f"rollback-{bad}".encode())
        r = c.post("/artifacts/sign", json=e)
        check(f"回退 {bad} 409", r.status_code == 409, r.json().get("detail", ""))

    print("\n== 7. 信任根轮换（旧根阈值批准） ==")
    new_roots_pub = [k.public_key_hex for k in new_root_keys]
    new_signers_pub = [new_signer.public_key_hex]

    def rotate_post(approvers):
        approvals = []
        for k in approvers:
            approvals.append(
                {
                    "key_id": k.kid,
                    "signature": crypto.sign_root_rotation(
                        private_key_hex=k.private_key_hex,
                        new_root_version=2,
                        new_threshold=2,
                        new_threshold_keys=new_roots_pub,
                        new_signer_keys=new_signers_pub,
                    ),
                }
            )
        return c.post(
            "/root/rotate",
            json={
                "new_root_version": 2,
                "new_threshold": 2,
                "new_threshold_public_keys": new_roots_pub,
                "new_signer_public_keys": new_signers_pub,
                "approvals": approvals,
            },
        )

    r = rotate_post([root_keys[0]])
    check("仅 1/2 批准 -> 403", r.status_code == 403, r.json().get("detail", ""))
    outsider = crypto.generate_keypair()
    bad = c.post(
        "/root/rotate",
        json={
            "new_root_version": 2,
            "new_threshold": 2,
            "new_threshold_public_keys": new_roots_pub,
            "new_signer_public_keys": new_signers_pub,
            "approvals": [
                {
                    "key_id": outsider.kid,
                    "signature": crypto.sign_root_rotation(
                        private_key_hex=outsider.private_key_hex,
                        new_root_version=2,
                        new_threshold=2,
                        new_threshold_keys=new_roots_pub,
                        new_signer_keys=new_signers_pub,
                    ),
                },
                {
                    "key_id": root_keys[0].kid,
                    "signature": crypto.sign_root_rotation(
                        private_key_hex=root_keys[0].private_key_hex,
                        new_root_version=2,
                        new_threshold=2,
                        new_threshold_keys=new_roots_pub,
                        new_signer_keys=new_signers_pub,
                    ),
                },
            ],
        },
    )
    check("局外密钥批准 -> 403", bad.status_code == 403, bad.json().get("detail", ""))
    r = rotate_post([root_keys[0], root_keys[1]])
    check("2/2 旧根批准 -> 200", r.status_code == 200, str(r.status_code))
    check("当前根版本=2", c.get("/root").json()["root_version"] == 2)

    print("\n== 8. 轮换后旧签名密钥失效 ==")
    old = envelope(signer_a, "firmware", "3.0.0", b"v3-signed-by-old")
    r = c.post("/artifacts/sign", json=old)
    check("旧签名者登记 403", r.status_code == 403, r.json().get("detail", ""))
    new_env = envelope(new_signer, "firmware", "3.0.0", b"v3-signed-by-new")
    r = c.post("/artifacts/sign", json=new_env)
    check("新签名者登记 201", r.status_code == 201, str(r.status_code))

    print("\n== 9. 旧制品验签：签名密码学有效但信任根已不含该密钥 ==")
    r = c.post(
        "/artifacts/verify",
        json={k: env[k] for k in ("artifact_type", "version", "digest", "nonce", "signature")},
    )
    body = r.json()
    check(
        "旧制品 valid=false & signer_trusted=false",
        body["valid"] is False and body["signer_trusted"] is False,
        body["reason"],
    )

    print(f"\n结果：{_passed} 通过，{_failed} 失败")
    return 1 if _failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except httpx.ConnectError:
        print(f"无法连接 {BASE}，请先启动服务：uvicorn app.main:app --port 8000")
        raise SystemExit(2)
