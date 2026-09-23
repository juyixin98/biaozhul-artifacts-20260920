"""加解密与版本权限测试。"""

import base64
import copy

import pytest

from keyvault import (
    DecryptError,
    KeyDestroyedError,
    KeyNotFoundError,
    NoActiveKeyError,
    WrongVersionError,
)


def test_encrypt_requires_active_key(svc):
    svc.generate_key()  # GENERATED 但未激活
    with pytest.raises(NoActiveKeyError):
        svc.encrypt(b"hello")


def test_roundtrip(active_svc):
    env = active_svc.encrypt(b"secret message")
    assert env["v"] == "v1"
    assert active_svc.decrypt(env) == b"secret message"


def test_ciphertext_is_not_plaintext(active_svc):
    env = active_svc.encrypt(b"secret message")
    raw = base64.b64decode(env["ct"])
    assert b"secret" not in raw


def test_decrypt_with_deactivated_key_allowed(active_svc):
    env = active_svc.encrypt(b"old data")
    active_svc.deactivate("v1")
    assert active_svc.decrypt(env) == b"old data"


def test_decrypt_with_generated_key_denied(svc):
    svc.generate_key()
    svc.activate("v1")
    env = svc.encrypt(b"x")
    svc.generate_key()  # v2 处于 GENERATED
    env2 = dict(env, v="v2")  # 伪造版本标签
    with pytest.raises(DecryptError):
        svc.decrypt(env2)


def test_destroyed_key_cannot_decrypt(active_svc):
    env = active_svc.encrypt(b"gone forever")
    active_svc.deactivate("v1")
    active_svc.destroy("v1")
    with pytest.raises(KeyDestroyedError):
        active_svc.decrypt(env)


def test_wrong_version_rejected(active_svc):
    env = active_svc.encrypt(b"data")
    active_svc.generate_key()
    active_svc.activate("v2")
    # 密文是 v1 加密的，却声明期望 v2 -> 拒绝
    with pytest.raises(WrongVersionError):
        active_svc.decrypt(env, expect_version="v2")
    # 声明正确版本则放行
    assert active_svc.decrypt(env, expect_version="v1") == b"data"


def test_unknown_version_in_envelope(active_svc):
    env = active_svc.encrypt(b"data")
    with pytest.raises(KeyNotFoundError):
        active_svc.decrypt(dict(env, v="v404"))


def test_tampered_ciphertext_rejected(active_svc):
    env = active_svc.encrypt(b"integrity matters")
    tampered = copy.deepcopy(env)
    raw = bytearray(base64.b64decode(tampered["ct"]))
    raw[0] ^= 0x01
    tampered["ct"] = base64.b64encode(bytes(raw)).decode()
    with pytest.raises(DecryptError):
        active_svc.decrypt(tampered)


def test_tampered_version_tag_rejected(active_svc):
    """把 v1 的密文改标为 v2：GCM 认证失败，必须拒绝。"""
    env = active_svc.encrypt(b"tag swap")
    active_svc.generate_key()
    active_svc.activate("v2")
    with pytest.raises(DecryptError):
        active_svc.decrypt(dict(env, v="v2"))


def test_aad_mismatch_rejected(active_svc):
    env = active_svc.encrypt(b"with aad", aad=b"context-1")
    with pytest.raises(DecryptError):
        active_svc.decrypt(env, aad=b"context-2")
    assert active_svc.decrypt(env, aad=b"context-1") == b"with aad"


def test_each_key_is_unique(svc):
    svc.generate_key()
    svc.generate_key()
    k1 = svc.store.load_key_material("v1")
    k2 = svc.store.load_key_material("v2")
    assert k1 != k2 and len(k1) == 32 and len(k2) == 32
