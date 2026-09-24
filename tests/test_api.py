"""HTTP 接口测试，覆盖全部验收场景。

验收点对应：
- 正文篡改          test_tampered_body_rejected
- 跨类型复用        test_cross_type_reuse_rejected
- 重复签名          test_duplicate_signature_rejected / test_nonce_replay_rejected
- 失效根            test_invalidated_root / test_unknown_signer_rejected
- 根轮换阈值批准    test_root_rotation_threshold_*
- 拒绝版本回退      test_version_rollback_rejected
"""

from __future__ import annotations

import importlib

from fastapi.testclient import TestClient

from app import crypto

from .conftest import rotate, sign_envelope

def test_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_operations_require_root(client):
    resp = client.get("/root")
    assert resp.status_code == 409
    payload = sign_envelope(crypto.generate_keypair(), "t", "1.0.0", b"x")
    assert client.post("/artifacts/sign", json=payload).status_code == 409


def test_root_init_twice_rejected(initialized_client):
    client = initialized_client
    resp = client.post(
        "/root/init",
        json={
            "root_version": 1,
            "threshold": 1,
            "threshold_public_keys": [crypto.generate_keypair().public_key_hex],
            "signer_public_keys": [crypto.generate_keypair().public_key_hex],
        },
    )
    assert resp.status_code == 409


def test_root_init_threshold_too_large(client, keys):
    resp = client.post(
        "/root/init",
        json={
            "root_version": 1,
            "threshold": 5,
            "threshold_public_keys": [k.public_key_hex for k in keys["root"]],
            "signer_public_keys": [k.public_key_hex for k in keys["signer"]],
        },
    )
    assert resp.status_code == 400


def test_happy_path_sign_and_verify(initialized_client, keys):
    client = initialized_client
    content = b"firmware-image-v1"
    payload = sign_envelope(keys["signer"][0], "firmware", "1.0.0", content)
    resp = client.post("/artifacts/sign", json=payload)
    assert resp.status_code == 201, resp.text
    record = resp.json()
    assert record["registered_at_root_version"] == 1

    # 有状态验签：重新提供当前摘要
    verify = client.post(
        "/artifacts/verify",
        json={
            "artifact_type": "firmware",
            "version": "1.0.0",
            "digest": payload["digest"],
            "nonce": payload["nonce"],
            "signature": payload["signature"],
        },
    )
    assert verify.status_code == 200
    body = verify.json()
    assert body["valid"] is True

    # 无状态验签（自带公钥）
    verify2 = client.post(
        "/artifacts/verify",
        json={
            "artifact_type": "firmware",
            "version": "1.0.0",
            "digest": payload["digest"],
            "nonce": payload["nonce"],
            "signature": payload["signature"],
            "public_key": keys["signer"][0].public_key_hex,
        },
    )
    assert verify2.json()["valid"] is True


def test_tampered_body_rejected(initialized_client, keys):
    """验收：正文篡改后摘要不匹配，验签必须 invalid。"""

    client = initialized_client
    payload = sign_envelope(keys["signer"][0], "firmware", "1.0.0", b"original body")
    assert client.post("/artifacts/sign", json=payload).status_code == 201

    tampered_digest = crypto.sha256_hex(b"tampered body")

    # 有状态路径：登记的签名对篡改摘要验签失败
    r1 = client.post(
        "/artifacts/verify",
        json={
            "artifact_type": "firmware",
            "version": "1.0.0",
            "digest": tampered_digest,
            "nonce": payload["nonce"],
            "signature": payload["signature"],
        },
    )
    assert r1.status_code == 200 and r1.json()["valid"] is False
    assert "篡改" in r1.json()["reason"]

    # 无状态路径同样失败
    r2 = client.post(
        "/artifacts/verify",
        json={
            "artifact_type": "firmware",
            "version": "1.0.0",
            "digest": tampered_digest,
            "nonce": payload["nonce"],
            "signature": payload["signature"],
            "public_key": keys["signer"][0].public_key_hex,
        },
    )
    assert r2.json()["valid"] is False

    # 篡改载荷直接登记也应被拒（签名与摘要不匹配）。
    # 用新版本+新 nonce，越过重复/nonce 检查，精确命中签名校验。
    p_tampered = sign_envelope(keys["signer"][0], "firmware", "1.0.1", b"original v2")
    p_tampered["digest"] = crypto.sha256_hex(b"tampered v2")
    r3 = client.post("/artifacts/sign", json=p_tampered)
    assert r3.status_code == 400, r3.text


def test_cross_type_reuse_rejected(initialized_client, keys):
    """验收：A 类型的签名搬到 B 类型必须失败。"""

    client = initialized_client
    payload = sign_envelope(keys["signer"][0], "firmware", "1.0.0", b"same bytes")
    assert client.post("/artifacts/sign", json=payload).status_code == 201

    # 用新 nonce 构造一个「按 firmware 类型签名、却声明为 container-image」的载荷，
    # 越过 nonce 查重，精确命中类型绑定的签名校验。
    reused = sign_envelope(keys["signer"][0], "firmware", "1.0.0", b"same bytes")
    reused["artifact_type"] = "container-image"
    resp = client.post("/artifacts/sign", json=reused)
    assert resp.status_code == 400, resp.text  # 签名不匹配

    # 验签端也能识别
    v = client.post(
        "/artifacts/verify",
        json={**reused, "public_key": keys["signer"][0].public_key_hex},
    )
    assert v.json()["valid"] is False


def test_duplicate_signature_rejected(initialized_client, keys):
    """验收：完全相同的签名载荷重复登记被拒（nonce 消费 + 制品唯一）。"""

    client = initialized_client
    payload = sign_envelope(keys["signer"][0], "firmware", "1.0.0", b"body")
    assert client.post("/artifacts/sign", json=payload).status_code == 201
    again = client.post("/artifacts/sign", json=payload)
    assert again.status_code == 409
    assert "重复" in again.json()["detail"] or "回退" in again.json()["detail"]


def test_nonce_replay_with_different_content_rejected(initialized_client, keys):
    """同一 nonce 换内容/换类型重放也必须拒绝。"""

    client = initialized_client
    p1 = sign_envelope(keys["signer"][0], "firmware", "1.0.0", b"body-1")
    assert client.post("/artifacts/sign", json=p1).status_code == 201

    # 不同类型、不同内容，但复用 nonce —— 先撞 nonce 查重
    content2 = b"body-2"
    digest2 = crypto.sha256_hex(content2)
    sig2 = crypto.sign_artifact(
        private_key_hex=keys["signer"][0].private_key_hex,
        digest_hex=digest2,
        artifact_type="addon",
        version="1.0.0",
        nonce_hex=p1["nonce"],
    )
    resp = client.post(
        "/artifacts/sign",
        json={
            "artifact_type": "addon",
            "version": "1.0.0",
            "digest": digest2,
            "key_id": keys["signer"][0].kid,
            "nonce": p1["nonce"],
            "signature": sig2,
        },
    )
    assert resp.status_code == 409
    assert "nonce" in resp.json()["detail"]


def test_version_rollback_rejected(initialized_client, keys):
    """验收：同一类型版本必须严格递增，回退（含相等）拒绝。"""

    client = initialized_client
    for ver in ["1.0.0", "1.2.0", "2.0.0"]:
        p = sign_envelope(keys["signer"][0], "firmware", ver, f"body-{ver}".encode())
        assert client.post("/artifacts/sign", json=p).status_code == 201

    for bad in ["1.9.9", "2.0.0", "1.2.0"]:
        p = sign_envelope(keys["signer"][0], "firmware", bad, f"body-{bad}".encode())
        resp = client.post("/artifacts/sign", json=p)
        assert resp.status_code == 409, (bad, resp.text)
        assert "回退" in resp.json()["detail"]

    # 不同类型各自独立计数
    other = sign_envelope(keys["signer"][0], "addon", "1.0.0", b"addon")
    assert client.post("/artifacts/sign", json=other).status_code == 201


def test_unknown_signer_rejected(initialized_client):
    """验收：不在信任根里的签名密钥登记被拒。"""

    client = initialized_client
    stranger = crypto.generate_keypair()
    payload = sign_envelope(stranger, "firmware", "1.0.0", b"x")
    resp = client.post("/artifacts/sign", json=payload)
    assert resp.status_code == 403


def test_root_rotation_threshold_happy_path(initialized_client, keys, new_keys):
    """旧根 2/3 批准后轮换成功，新密钥生效、旧签名者失效。"""

    client = initialized_client
    # 先用旧签名者登记 1.0.0
    old_payload = sign_envelope(keys["signer"][0], "firmware", "1.0.0", b"v1")
    assert client.post("/artifacts/sign", json=old_payload).status_code == 201

    resp = rotate(client, keys["root"], new_keys, new_version=2, signer_indexes=[0, 1])
    assert resp.status_code == 200, resp.text
    assert resp.json()["root_version"] == 2

    root = client.get("/root").json()
    assert set(root["signer_keys"].keys()) == {new_keys["signer"][0].kid}

    # 旧签名者已失效
    old_again = sign_envelope(keys["signer"][0], "firmware", "2.0.0", b"v2")
    assert client.post("/artifacts/sign", json=old_again).status_code == 403

    # 新签名者可以登记
    new_payload = sign_envelope(new_keys["signer"][0], "firmware", "2.0.0", b"v2")
    assert client.post("/artifacts/sign", json=new_payload).status_code == 201

    # 旧制品验签：密码学仍有效，但签名者已不在当前根 -> invalid（失效根/失效密钥）
    v = client.post(
        "/artifacts/verify",
        json={
            "artifact_type": "firmware",
            "version": "1.0.0",
            "digest": old_payload["digest"],
            "nonce": old_payload["nonce"],
            "signature": old_payload["signature"],
        },
    )
    assert v.status_code == 200
    assert v.json()["valid"] is False
    assert v.json()["signer_trusted"] is False


def test_root_rotation_below_threshold_rejected(initialized_client, keys, new_keys):
    """验收：只有 1 个批准（阈值 2）时轮换被拒，旧根保持不变。"""

    client = initialized_client
    resp = rotate(client, keys["root"], new_keys, new_version=2, signer_indexes=[0])
    assert resp.status_code == 403
    assert "批准不足" in resp.json()["detail"]
    assert client.get("/root").json()["root_version"] == 1


def test_root_rotation_non_member_rejected(initialized_client, keys, new_keys):
    """验收：非旧根成员（含已轮换失效的密钥）的批准无效。"""

    client = initialized_client
    outsider = crypto.generate_keypair()
    resp = rotate(
        client,
        keys["root"],
        new_keys,
        new_version=2,
        signer_indexes=[0, 1],
    )
    # 正常轮换一次
    assert resp.status_code == 200

    # 第二次轮换：用「旧根但已失效」的成员 + 新根成员都试试
    resp2 = rotate(client, keys["root"], new_keys, new_version=3, signer_indexes=[0, 1])
    assert resp2.status_code == 403
    assert "不是当前信任根的阈值成员" in resp2.json()["detail"]

    # 完全局外的密钥也不行
    new_version3_keys = {
        "root": [crypto.generate_keypair()],
        "signer": [crypto.generate_keypair()],
    }
    bad_sig = crypto.sign_root_rotation(
        private_key_hex=outsider.private_key_hex,
        new_root_version=3,
        new_threshold=1,
        new_threshold_keys=[new_version3_keys["root"][0].public_key_hex],
        new_signer_keys=[new_version3_keys["signer"][0].public_key_hex],
    )
    r = client.post(
        "/root/rotate",
        json={
            "new_root_version": 3,
            "new_threshold": 1,
            "new_threshold_public_keys": [new_version3_keys["root"][0].public_key_hex],
            "new_signer_public_keys": [new_version3_keys["signer"][0].public_key_hex],
            "approvals": [{"key_id": outsider.kid, "signature": bad_sig}],
        },
    )
    assert r.status_code == 403
    assert client.get("/root").json()["root_version"] == 2


def test_root_version_rollback_rejected(initialized_client, keys, new_keys):
    """验收：根版本号必须严格递增。"""

    client = initialized_client
    resp = rotate(client, keys["root"], new_keys, new_version=1, signer_indexes=[0, 1])
    assert resp.status_code == 409
    assert "回退" in resp.json()["detail"]


def test_rotation_approval_tampering_rejected(initialized_client, keys, new_keys):
    """批准签名绑定新根内容：客户端篡改新阈值公钥后批准验不过。"""

    client = initialized_client
    # 两个旧根成员对「原内容」签名
    approvals = []
    for i in [0, 1]:
        approvals.append(
            {
                "key_id": keys["root"][i].kid,
                "signature": crypto.sign_root_rotation(
                    private_key_hex=keys["root"][i].private_key_hex,
                    new_root_version=2,
                    new_threshold=2,
                    new_threshold_keys=[k.public_key_hex for k in new_keys["root"]],
                    new_signer_keys=[k.public_key_hex for k in new_keys["signer"]],
                ),
            }
        )
    # 发送时偷偷换掉一个新根公钥
    swapped = list(new_keys["root"])
    swapped[0] = crypto.generate_keypair()
    resp = client.post(
        "/root/rotate",
        json={
            "new_root_version": 2,
            "new_threshold": 2,
            "new_threshold_public_keys": [k.public_key_hex for k in swapped],
            "new_signer_public_keys": [k.public_key_hex for k in new_keys["signer"]],
            "approvals": approvals,
        },
    )
    assert resp.status_code == 403
    assert "批准签名无效" in resp.json()["detail"]


def test_duplicate_approval_rejected(initialized_client, keys, new_keys):
    """同一成员重复批准直接拒绝。"""

    client = initialized_client
    resp = rotate(client, keys["root"], new_keys, new_version=2, signer_indexes=[0, 0])
    assert resp.status_code == 400


def test_state_persistence_roundtrip(keys, monkeypatch, tmp_path):
    """设置 STATE_PATH 后，重启进程（重建 app）状态仍在。"""

    state_file = str(tmp_path / "state.json")
    monkeypatch.setenv("STATE_PATH", state_file)

    from app import main as main_module

    importlib.reload(main_module)
    c1 = TestClient(main_module.app)
    r = c1.post(
        "/root/init",
        json={
            "root_version": 1,
            "threshold": 2,
            "threshold_public_keys": [k.public_key_hex for k in keys["root"]],
            "signer_public_keys": [k.public_key_hex for k in keys["signer"]],
        },
    )
    assert r.status_code == 201
    payload = sign_envelope(keys["signer"][0], "firmware", "1.0.0", b"persisted")
    assert c1.post("/artifacts/sign", json=payload).status_code == 201

    # 模拟重启：重新加载模块
    importlib.reload(main_module)
    c2 = TestClient(main_module.app)
    assert c2.get("/root").json()["root_version"] == 1
    v = c2.post(
        "/artifacts/verify",
        json={
            "artifact_type": "firmware",
            "version": "1.0.0",
            "digest": payload["digest"],
            "nonce": payload["nonce"],
            "signature": payload["signature"],
        },
    )
    assert v.json()["valid"] is True

    # 回退保护也持久化了
    replay = sign_envelope(keys["signer"][0], "firmware", "1.0.0", b"persisted")
    assert c2.post("/artifacts/sign", json=replay).status_code == 409


def test_malformed_inputs_400(initialized_client, keys):
    client = initialized_client
    payload = sign_envelope(keys["signer"][0], "firmware", "1.0.0", b"x")
    payload["digest"] = "not-hex"
    assert client.post("/artifacts/sign", json=payload).status_code == 400

    # 非法版本号由服务端校验拒绝（400）：用一个不经过版本检查的原始载荷构造
    digest = crypto.sha256_hex(b"y")
    nonce = crypto.generate_nonce()
    r = client.post(
        "/artifacts/sign",
        json={
            "artifact_type": "firmware",
            "version": "v1.0.1",  # 非法：带 v 前缀
            "digest": digest,
            "key_id": keys["signer"][0].kid,
            "nonce": nonce,
            "signature": "00" * 64,  # 占位，版本检查先失败
        },
    )
    assert r.status_code == 400
    assert "版本" in r.json()["detail"]
