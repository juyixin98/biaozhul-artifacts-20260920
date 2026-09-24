"""制品签名验证测试：正文篡改、跨类型复用、版本绑定、重复签名、失效签名者等。"""
from __future__ import annotations

import base64

import pytest

from scripts.sighelp import (
    b64e,
    make_envelope,
    sha256_hex,
    sign_artifact_block,
)

CONTENT = b"artifact-body-v1"
ATYPE = "report"
AVER = "1.0.0"


def verify_payload(client, content, atype, aver, privs, **overrides):
    env = make_envelope(content, atype, aver, privs).model_dump()
    env.update(overrides)
    return client.post(
        "/verify",
        json={"envelope": env, "content_base64": b64e(content)},
    )


def test_valid_envelope_accepted(bootstrapped, parties):
    resp = verify_payload(
        bootstrapped, CONTENT, ATYPE, AVER, parties["art_priv"][:2]
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["accepted"] is True
    assert body["valid_signatures"] == 2
    assert body["threshold"] == 2


def test_below_threshold_rejected(bootstrapped, parties):
    resp = verify_payload(
        bootstrapped, CONTENT, ATYPE, AVER, parties["art_priv"][:1]
    )
    body = resp.json()
    assert body["accepted"] is False
    assert body["valid_signatures"] == 1
    assert body["reason"] == "threshold_not_met_or_invalid_signature"


# ---- 验收点 1：正文篡改 ----

def test_content_tampered_detected(bootstrapped, parties):
    env = make_envelope(CONTENT, ATYPE, AVER, parties["art_priv"][:2]).model_dump()
    tampered = b"artifact-body-V2-tampered"
    resp = bootstrapped.post(
        "/verify",
        json={"envelope": env, "content_base64": b64e(tampered)},
    )
    assert resp.json()["accepted"] is False
    assert resp.json()["reason"] == "digest_mismatch_content_tampered"


def test_digest_field_tampered_detected(bootstrapped, parties):
    resp = verify_payload(
        bootstrapped,
        CONTENT,
        ATYPE,
        AVER,
        parties["art_priv"][:2],
        digest="00" * 32,
    )
    body = resp.json()
    assert body["accepted"] is False
    # 摘要改了 -> 重算不匹配
    assert body["reason"] == "digest_mismatch_content_tampered"


def test_signature_against_tampered_digest_fails(bootstrapped, parties):
    # 不提供正文，只把 digest 换成攻击者目标内容的摘要：
    # 原签名绑定的是旧摘要，故签名校验失败。
    env = make_envelope(CONTENT, ATYPE, AVER, parties["art_priv"][:2]).model_dump()
    env["digest"] = sha256_hex(b"attacker content")
    resp = bootstrapped.post("/verify", json={"envelope": env})
    body = resp.json()
    assert body["accepted"] is False
    assert all(b["reason"] == "signature_mismatch" for b in body["blocks"])


# ---- 验收点 2：跨类型/版本复用 ----

def test_cross_type_signature_rejected(bootstrapped, parties):
    # 签名针对 report，请求里却声称是 container-image
    env = make_envelope(CONTENT, ATYPE, AVER, parties["art_priv"][:2]).model_dump()
    env["artifact_type"] = "container-image"
    resp = bootstrapped.post(
        "/verify",
        json={"envelope": env, "content_base64": b64e(CONTENT)},
    )
    body = resp.json()
    assert body["accepted"] is False
    assert all(b["reason"] == "signature_mismatch" for b in body["blocks"])


def test_cross_version_signature_rejected(bootstrapped, parties):
    # 签名针对 1.0.0，请求里却声称 0.9.0
    env = make_envelope(CONTENT, ATYPE, AVER, parties["art_priv"][:2]).model_dump()
    env["version"] = "0.9.0"
    resp = bootstrapped.post(
        "/verify",
        json={"envelope": env, "content_base64": b64e(CONTENT)},
    )
    body = resp.json()
    assert body["accepted"] is False
    assert all(b["reason"] == "signature_mismatch" for b in body["blocks"])


def test_signature_not_reusable_for_other_content(bootstrapped, parties):
    env = make_envelope(CONTENT, ATYPE, AVER, parties["art_priv"][:2]).model_dump()
    # 保留签名块，仅替换 digest 为另一份内容
    other = b"completely-different-content"
    env["digest"] = sha256_hex(other)
    resp = bootstrapped.post("/verify", json={"envelope": env})
    assert resp.json()["accepted"] is False


# ---- 验收点 3：重复签名 ----

def test_duplicate_signer_block_counts_once(bootstrapped, parties):
    env = make_envelope(CONTENT, ATYPE, AVER, parties["art_priv"][:1]).model_dump()
    env["signatures"].append(dict(env["signatures"][0]))  # 同一块放两遍
    resp = bootstrapped.post(
        "/verify",
        json={"envelope": env, "content_base64": b64e(CONTENT)},
    )
    body = resp.json()
    assert body["accepted"] is False  # 只有 1 个独立签名者，阈值为 2
    reasons = {b["reason"] for b in body["blocks"]}
    assert "duplicate_signer_block" in reasons
    assert body["valid_signatures"] == 1


def test_register_rejects_duplicate_registration(bootstrapped, parties):
    env = make_envelope(CONTENT, ATYPE, AVER, parties["art_priv"][:2]).model_dump()
    payload = {"envelope": env, "content_base64": b64e(CONTENT)}
    r1 = bootstrapped.post("/artifacts/register", json=payload)
    assert r1.status_code == 201
    r2 = bootstrapped.post("/artifacts/register", json=payload)
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "artifact_already_registered"


def test_re_register_with_overlapping_signers_rejected(bootstrapped, parties):
    """同一制品已登记后，即便换一批部分重叠的签名者重新提交，仍被拒绝。"""
    env = make_envelope(CONTENT, ATYPE, AVER, parties["art_priv"][:2]).model_dump()
    bootstrapped.post(
        "/artifacts/register",
        json={"envelope": env, "content_base64": b64e(CONTENT)},
    )
    # signer1（已签过）+ signer3：阈值满足，但制品已登记
    block1 = sign_artifact_block(
        parties["art_priv"][0], sha256_hex(CONTENT), ATYPE, AVER
    )
    block3 = sign_artifact_block(
        parties["art_priv"][2], sha256_hex(CONTENT), ATYPE, AVER
    )
    env2 = dict(env)
    env2["signatures"] = [block1.model_dump(), block3.model_dump()]
    r = bootstrapped.post(
        "/artifacts/register",
        json={"envelope": env2, "content_base64": b64e(CONTENT)},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "artifact_already_registered"


# ---- 验收点 4：失效根 / 未授权角色 ----

def test_root_key_cannot_sign_artifacts(bootstrapped, parties):
    resp = verify_payload(
        bootstrapped,
        CONTENT,
        ATYPE,
        AVER,
        [parties["root_priv"][0], parties["art_priv"][0]],
    )
    body = resp.json()
    assert body["accepted"] is False  # 只有 1 个授权制品签名者
    reasons = {b.get("reason") for b in body["blocks"]}
    assert "signer_not_authorized" in reasons


def test_unknown_key_rejected(bootstrapped, parties):
    outsider = parties["new_art_priv"][0]
    resp = verify_payload(
        bootstrapped,
        CONTENT,
        ATYPE,
        AVER,
        [outsider, parties["art_priv"][0]],
    )
    body = resp.json()
    assert body["accepted"] is False
    reasons = {b.get("reason") for b in body["blocks"]}
    assert "signer_not_authorized" in reasons


def test_garbage_signature_and_encoding(bootstrapped, parties):
    env = make_envelope(CONTENT, ATYPE, AVER, parties["art_priv"][:1]).model_dump()
    env["signatures"][0]["signature"] = b64e(b"\x00" * 64)
    r = bootstrapped.post(
        "/verify",
        json={"envelope": env, "content_base64": b64e(CONTENT)},
    )
    assert r.json()["accepted"] is False
    assert r.json()["blocks"][0]["reason"] == "signature_mismatch"

    env["signatures"][0]["signature"] = "!!!not-base64!!!"
    r = bootstrapped.post(
        "/verify",
        json={"envelope": env, "content_base64": b64e(CONTENT)},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_encoding"


def test_register_tampered_content_rejected(bootstrapped, parties):
    env = make_envelope(CONTENT, ATYPE, AVER, parties["art_priv"][:2]).model_dump()
    r = bootstrapped.post(
        "/artifacts/register",
        json={"envelope": env, "content_base64": b64e(b"tampered body")},
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "digest_mismatch_content_tampered"
