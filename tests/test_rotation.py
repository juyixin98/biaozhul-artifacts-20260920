"""信任根轮换测试：阈值批准、角色限制、版本回退拒绝、旧根失效。"""
from __future__ import annotations

from scripts.sighelp import (
    approval_block,
    b64e,
    make_envelope,
    make_root_body,
)

CONTENT = b"artifact-after-rotation"


def _new_root_v2(parties, version=2, root_threshold=2, artifact_threshold=2):
    return make_root_body(
        version,
        root_threshold,
        artifact_threshold,
        parties["new_root_pub"],
        parties["new_art_pub"],
    )


def test_rotate_requires_threshold(bootstrapped, parties):
    v2 = _new_root_v2(parties)
    # 只有 1 个旧根批准，阈值 2
    approvals = [approval_block(parties["root_priv"][0], v2)]
    r = bootstrapped.post(
        "/roots/rotate",
        json={"new_root": v2.model_dump(), "approvals": [a.model_dump() for a in approvals]},
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "rotation_unauthorized"


def test_rotate_succeeds_with_threshold(bootstrapped, parties):
    v2 = _new_root_v2(parties)
    approvals = [approval_block(p, v2) for p in parties["root_priv"][:2]]
    r = bootstrapped.post(
        "/roots/rotate",
        json={"new_root": v2.model_dump(), "approvals": [a.model_dump() for a in approvals]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 2

    cur = bootstrapped.get("/roots/current").json()
    new_kids = {s["key_id"] for s in cur["artifact_signers"]}
    expected = {
        __import__("app").keys.key_id(p) for p in parties["new_art_pub"]
    }
    assert new_kids == expected


def test_rotate_rejects_version_rollback(bootstrapped, parties):
    # 先合法轮换到 v2
    v2 = _new_root_v2(parties)
    approvals = [approval_block(p, v2) for p in parties["root_priv"][:2]]
    bootstrapped.post(
        "/roots/rotate",
        json={"new_root": v2.model_dump(), "approvals": [a.model_dump() for a in approvals]},
    )
    # 再提交 version=1（回退）
    v1 = make_root_body(1, 2, 2, parties["root_pub"], parties["art_pub"])
    old_approvals = [approval_block(p, v1) for p in parties["root_priv"][:2]]
    r = bootstrapped.post(
        "/roots/rotate",
        json={"new_root": v1.model_dump(), "approvals": [a.model_dump() for a in old_approvals]},
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "version_rollback_rejected"
    # 当前根仍是 v2
    assert bootstrapped.get("/roots/current").json()["version"] == 2


def test_rotate_rejects_same_version(bootstrapped, parties):
    v1_copy = make_root_body(1, 2, 2, parties["new_root_pub"], parties["new_art_pub"])
    approvals = [approval_block(p, v1_copy) for p in parties["root_priv"][:2]]
    r = bootstrapped.post(
        "/roots/rotate",
        json={"new_root": v1_copy.model_dump(), "approvals": [a.model_dump() for a in approvals]},
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "version_rollback_rejected"


def test_rotate_rejects_skipped_version(bootstrapped, parties):
    v3 = _new_root_v2(parties, version=3)
    approvals = [approval_block(p, v3) for p in parties["root_priv"][:2]]
    r = bootstrapped.post(
        "/roots/rotate",
        json={"new_root": v3.model_dump(), "approvals": [a.model_dump() for a in approvals]},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_root_version"


def test_artifacts_signer_cannot_approve_rotation(bootstrapped, parties):
    v2 = _new_root_v2(parties)
    # 用制品角色密钥批准（即便数量够）也不通过
    from app import keys as keyutil
    art_more = [keyutil.generate_keypair()[0] for _ in range(2)]
    approvals = [approval_block(p, v2) for p in art_more[:2]]
    r = bootstrapped.post(
        "/roots/rotate",
        json={"new_root": v2.model_dump(), "approvals": [a.model_dump() for a in approvals]},
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "rotation_unauthorized"


def test_new_root_key_cannot_approve(bootstrapped, parties):
    v2 = _new_root_v2(parties)
    # 新根自己的密钥（尚不在信任链上）没有批准权
    approvals = [approval_block(p, v2) for p in parties["new_root_priv"][:2]]
    r = bootstrapped.post(
        "/roots/rotate",
        json={"new_root": v2.model_dump(), "approvals": [a.model_dump() for a in approvals]},
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "rotation_unauthorized"


def test_duplicate_approval_counts_once(bootstrapped, parties):
    v2 = _new_root_v2(parties)
    block = approval_block(parties["root_priv"][0], v2)
    r = bootstrapped.post(
        "/roots/rotate",
        json={
            "new_root": v2.model_dump(),
            "approvals": [block.model_dump(), block.model_dump()],
        },
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "rotation_unauthorized"


def test_approval_bound_to_exact_descriptor(bootstrapped, parties):
    v2 = _new_root_v2(parties)
    # 对另一个描述符（阈值不同）的批准不能用于 v2
    other = _new_root_v2(parties, artifact_threshold=1)
    approvals = [approval_block(p, other) for p in parties["root_priv"][:2]]
    r = bootstrapped.post(
        "/roots/rotate",
        json={"new_root": v2.model_dump(), "approvals": [a.model_dump() for a in approvals]},
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "rotation_unauthorized"


def test_after_rotation_old_artifact_signers_invalid(bootstrapped, parties):
    v2 = _new_root_v2(parties)
    approvals = [approval_block(p, v2) for p in parties["root_priv"][:2]]
    bootstrapped.post(
        "/roots/rotate",
        json={"new_root": v2.model_dump(), "approvals": [a.model_dump() for a in approvals]},
    )
    # 旧制品密钥签名：在新根下不再被信任
    env = make_envelope(CONTENT, "report", "2.0.0", parties["art_priv"][:2])
    r = bootstrapped.post(
        "/verify",
        json={"envelope": env.model_dump(), "content_base64": b64e(CONTENT)},
    )
    body = r.json()
    assert body["accepted"] is False
    assert body["root_version"] == 2
    assert all(b["reason"] == "signer_not_authorized" for b in body["blocks"])

    # 新制品密钥签名：通过
    env2 = make_envelope(CONTENT, "report", "2.0.0", parties["new_art_priv"][:2])
    r2 = bootstrapped.post(
        "/verify",
        json={"envelope": env2.model_dump(), "content_base64": b64e(CONTENT)},
    )
    assert r2.json()["accepted"] is True


def test_chained_rotation_v2_to_v3(bootstrapped, parties):
    # v1 -> v2
    v2 = _new_root_v2(parties)
    ap2 = [approval_block(p, v2) for p in parties["root_priv"][:2]]
    assert bootstrapped.post(
        "/roots/rotate",
        json={"new_root": v2.model_dump(), "approvals": [a.model_dump() for a in ap2]},
    ).status_code == 200
    # v2 -> v3（由 v2 的根密钥批准）
    from app import keys as keyutil
    v3_priv = [keyutil.generate_keypair()[0] for _ in range(3)]
    v3_pub = [p.public_key() for p in v3_priv]
    v3_art_priv = [keyutil.generate_keypair()[0] for _ in range(2)]
    v3 = make_root_body(3, 2, 2, v3_pub, [p.public_key() for p in v3_art_priv])
    ap3 = [approval_block(p, v3) for p in parties["new_root_priv"][:2]]
    r = bootstrapped.post(
        "/roots/rotate",
        json={"new_root": v3.model_dump(), "approvals": [a.model_dump() for a in ap3]},
    )
    assert r.status_code == 200
    assert bootstrapped.get("/roots/current").json()["version"] == 3

    # v1 旧根密钥不能再批准
    stale = make_root_body(4, 2, 2, v3_pub, [p.public_key() for p in v3_art_priv])
    ap_stale = [approval_block(p, stale) for p in parties["root_priv"][:2]]
    r2 = bootstrapped.post(
        "/roots/rotate",
        json={"new_root": stale.model_dump(), "approvals": [a.model_dump() for a in ap_stale]},
    )
    assert r2.status_code == 403


def test_bootstrap_rules(client, parties):
    # 未引导时验证返回 not_bootstrapped
    env = make_envelope(b"x", "t", "1", parties["art_priv"][:2])
    r = client.post("/verify", json={"envelope": env.model_dump()})
    assert r.json()["accepted"] is False
    assert r.json()["reason"] == "not_bootstrapped"

    body = make_root_body(5, 2, 2, parties["root_pub"], parties["art_pub"])
    r = client.post("/roots/bootstrap", json=body.model_dump())
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_root_version"

    body = make_root_body(1, 2, 2, parties["root_pub"], parties["art_pub"])
    assert client.post("/roots/bootstrap", json=body.model_dump()).status_code == 201
    # 重复引导被拒绝
    r = client.post("/roots/bootstrap", json=body.model_dump())
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "already_bootstrapped"


def test_threshold_and_role_separation_validation(client, parties):
    # 阈值超过签名者数
    bad = make_root_body(1, 9, 2, parties["root_pub"], parties["art_pub"])
    r = client.post("/roots/bootstrap", json=bad.model_dump())
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "threshold_too_large"

    # 根/制品角色公钥重叠
    overlap_pubs = [parties["art_pub"][0], parties["root_pub"][1], parties["root_pub"][2]]
    bad2 = make_root_body(1, 2, 2, overlap_pubs, parties["art_pub"])
    r = client.post("/roots/bootstrap", json=bad2.model_dump())
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "role_separation_violation"


def test_rotate_before_bootstrap(client, parties):
    v2 = _new_root_v2(parties)
    approvals = [approval_block(p, v2) for p in parties["root_priv"][:2]]
    r = client.post(
        "/roots/rotate",
        json={"new_root": v2.model_dump(), "approvals": [a.model_dump() for a in approvals]},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "not_bootstrapped"
