"""Ed25519 签名包与规范化 JSON 测试。"""
from __future__ import annotations

import copy
import json

import pytest

from app.crypto import (
    canonicalize,
    generate_keypair,
    private_key_to_pem,
    public_key_to_pem,
    load_private_key_pem,
    load_public_key_pem,
    sign_policies,
    verify_bundle,
)
from app.errors import BundleError, SignatureError


@pytest.fixture(scope="module")
def keys():
    priv, pub = generate_keypair()
    return priv, pub


def test_pem_roundtrip(keys):
    priv, pub = keys
    assert load_private_key_pem(private_key_to_pem(priv)) is not None
    assert load_public_key_pem(public_key_to_pem(pub)) is not None


def test_canonical_json_key_order_invariant_and_no_whitespace():
    a = canonicalize({"b": 1, "a": [1, 2, {"z": 0, "y": 9}]})
    b = canonicalize({"a": [1, 2, {"y": 9, "z": 0}], "b": 1})
    assert a == b
    assert b" " not in a and b"\n" not in a
    # 中文等 UTF-8 字符按码点排序稳定
    c1 = canonicalize({"甲": 1, "a": 2})
    c2 = canonicalize({"a": 2, "甲": 1})
    assert c1 == c2


def test_canonical_rejects_non_json_types():
    with pytest.raises(TypeError):
        canonicalize({"a": object()})
    with pytest.raises(TypeError):
        canonicalize((1, 2))
    with pytest.raises(ValueError):
        canonicalize(float("nan"))


def test_sign_and_verify_happy_path(keys):
    priv, pub = keys
    policies = [{"id": "p1", "rules": [
        {"id": "r1", "effect": "allow",
         "condition": {"op": "eq", "args": [{"attr": "subject.a"}, {"literal": 1}]}}]}]
    bundle = sign_policies(policies, priv, kid="k1")
    assert bundle["alg"] == "Ed25519"
    assert bundle["kid"] == "k1"
    assert verify_bundle(bundle, pub) == policies


def test_tampered_policy_fails_verification(keys):
    priv, pub = keys
    policies = [{"id": "p1", "rules": [
        {"id": "r1", "effect": "allow", "condition": {"literal": True}}]}]
    bundle = sign_policies(policies, priv, kid="k1")

    # 篡改 1：把 allow 改成 deny
    tampered = copy.deepcopy(bundle)
    tampered["policies"][0]["rules"][0]["effect"] = "deny"
    with pytest.raises(SignatureError):
        verify_bundle(tampered, pub)

    # 篡改 2：偷偷增加一条规则
    tampered2 = copy.deepcopy(bundle)
    tampered2["policies"][0]["rules"].append(
        {"id": "r-backdoor", "effect": "allow", "condition": {"literal": True}})
    with pytest.raises(SignatureError):
        verify_bundle(tampered2, pub)

    # 篡改 3：键重排不影响验签（签名建立在规范化序列化之上）
    reordered = copy.deepcopy(bundle)
    reordered["policies"] = json.loads(json.dumps(reordered["policies"], sort_keys=False))
    reordered["policies"][0] = {
        "rules": reordered["policies"][0]["rules"],
        "id": "p1",
    }
    assert verify_bundle(reordered, pub) is not None


def test_signature_from_other_key_rejected(keys):
    priv, pub = keys
    other_priv, _other_pub = generate_keypair()
    bundle = sign_policies([{"id": "p"}], other_priv, kid="attacker")
    with pytest.raises(SignatureError):
        verify_bundle(bundle, pub)


def test_bundle_structure_validation(keys):
    priv, pub = keys
    good = sign_policies([{"id": "p"}], priv, kid="k1")

    for mutate, expected in [
        (lambda b: b.update({"version": 2}), BundleError),
        (lambda b: b.update({"alg": "RS256"}), BundleError),
        (lambda b: b.update({"canonical": "https://other"}), BundleError),
        (lambda b: b.update({"kid": ""}), BundleError),
        (lambda b: b.update({"extra": 1}), BundleError),
        (lambda b: b.update({"signature": "not-base64!!"}) , BundleError),
    ]:
        bad = copy.deepcopy(good)
        mutate(bad)
        with pytest.raises(expected):
            verify_bundle(bad, pub)

    with pytest.raises(BundleError):
        verify_bundle(["not", "an", "object"], pub)
    with pytest.raises(SignatureError):
        bad = copy.deepcopy(good)
        bad["signature"] = "AAAA"
        verify_bundle(bad, pub)
