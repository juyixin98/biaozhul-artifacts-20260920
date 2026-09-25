"""密码原语测试：AES-GCM 信封、篡改检测、KEK 包装。"""

from __future__ import annotations

import os

import pytest

from keyversion import crypto
from keyversion.errors import InvalidEnvelope


def test_encrypt_decrypt_roundtrip():
    key = crypto.generate_key()
    env = crypto.encrypt(key, "v0001", b"secret payload")
    assert crypto.parse_envelope(env) == "v0001"
    assert crypto.decrypt(key, env) == b"secret payload"


def test_ciphertext_is_nondeterministic():
    key = crypto.generate_key()
    a = crypto.encrypt(key, "v1", b"same")
    b = crypto.encrypt(key, "v1", b"same")
    # 随机 nonce：相同明文密文不同
    assert a != b
    assert crypto.decrypt(key, a) == crypto.decrypt(key, b) == b"same"


def test_wrong_key_rejected():
    env = crypto.encrypt(crypto.generate_key(), "v1", b"data")
    with pytest.raises(InvalidEnvelope):
        crypto.decrypt(crypto.generate_key(), env)


def test_envelope_version_binding_defeats_version_swap():
    """把信封里的版本号改成另一个版本 -> AAD 不匹配，认证失败。"""
    k1 = crypto.generate_key()
    env = crypto.encrypt(k1, "v0001-aaaa", b"data")
    vlen = int.from_bytes(env[4:6], "big")
    end = 6 + vlen
    # v0001-aaaa 与 v0001-bbbb 等长，直接替换版本号字节
    swapped = env[:6] + b"v0001-bbbb" + env[end:]
    with pytest.raises(InvalidEnvelope):
        crypto.decrypt(k1, swapped)


def test_tampered_ciphertext_byte_rejected():
    key = crypto.generate_key()
    env = bytearray(crypto.encrypt(key, "v1", b"authenticated"))
    env[-1] ^= 0xFF
    with pytest.raises(InvalidEnvelope):
        crypto.decrypt(key, bytes(env))


def test_truncated_envelope_rejected():
    key = crypto.generate_key()
    env = crypto.encrypt(key, "v1", b"data")
    with pytest.raises(InvalidEnvelope):
        crypto.decrypt(key, env[:10])
    with pytest.raises(InvalidEnvelope):
        crypto.parse_envelope(b"XXXX" + env[4:])


def test_empty_and_large_plaintext():
    key = crypto.generate_key()
    assert crypto.decrypt(key, crypto.encrypt(key, "v1", b"")) == b""
    big = bytes(range(256)) * 100
    assert crypto.decrypt(key, crypto.encrypt(key, "v1", big)) == big


def test_wrap_unwrap_dek_roundtrip():
    kek = os.urandom(32)
    dek = crypto.generate_key()
    blob = crypto.wrap_dek(kek, dek)
    assert crypto.unwrap_dek(kek, blob) == dek
    with pytest.raises(InvalidEnvelope):
        crypto.unwrap_dek(os.urandom(32), blob)


def test_fingerprint_stable_and_distinct():
    k = crypto.generate_key()
    assert crypto.key_fingerprint(k) == crypto.key_fingerprint(k)
    assert crypto.key_fingerprint(k) != crypto.key_fingerprint(crypto.generate_key())
    assert len(crypto.key_fingerprint(k)) == 64
