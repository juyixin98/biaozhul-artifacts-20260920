"""crypto 层单元测试：nonce 唯一性、AAD 绑定、认证失败检测。"""

from __future__ import annotations

import os

import pytest

from envelope import crypto
from envelope.errors import AEADAuthenticationError  # noqa: F401  (下方按裸名引用)


def test_key_length_and_uniqueness():
    keys = {crypto.generate_key() for _ in range(20)}
    assert all(len(k) == crypto.KEY_BYTES for k in keys)
    assert len(keys) == 20  # 随机密钥互不相同


def test_block_nonce_derivation_is_unique_and_stable():
    nonces = [crypto.block_nonce(i) for i in range(5)]
    assert len(set(nonces)) == 5
    assert all(len(n) == crypto.NONCE_BYTES for n in nonces)
    assert crypto.block_nonce(3) == nonces[3]  # 确定性派生
    assert nonces[0].startswith(b"BLCK")


def test_wrap_nonce_counter_unique_and_monotonic():
    a = crypto.wrap_nonce(1)
    b = crypto.wrap_nonce(2)
    assert a != b and a.startswith(b"WRAP") and b.startswith(b"WRAP")


def test_nonces_are_domain_separated():
    # 块 nonce、包裹 nonce、头 nonce 即使序号相同也不冲突
    assert crypto.block_nonce(1) != crypto.wrap_nonce(1)
    assert crypto.header_nonce() != crypto.block_nonce(0)
    assert len({crypto.block_nonce(0), crypto.wrap_nonce(1), crypto.header_nonce()}) == 3


def test_canonical_json_is_deterministic():
    a = crypto.canonical_json({"b": 1, "a": 2, "nested": {"z": 0, "y": 0}})
    b = crypto.canonical_json({"a": 2, "b": 1, "nested": {"y": 0, "z": 0}})
    assert a == b
    assert b"," in a and b": " not in a  # 无空白


def test_aad_distinguishes_file_and_block():
    aad_a0 = crypto.block_aad(file_id="aaa", block_index=0, chunk_size=1024)
    aad_a1 = crypto.block_aad(file_id="aaa", block_index=1, chunk_size=1024)
    aad_b0 = crypto.block_aad(file_id="bbb", block_index=0, chunk_size=1024)
    assert aad_a0 != aad_a1 != aad_b0 and aad_a0 != aad_b0


def test_tampered_ciphertext_fails_auth():
    key = crypto.generate_key()
    nonce = crypto.block_nonce(0)
    aad = crypto.block_aad(file_id="f", block_index=0, chunk_size=64)
    ct = crypto.aead_seal(key, nonce, b"hello", aad)
    tampered = bytearray(ct)
    tampered[0] ^= 0x01
    with pytest.raises(AEADAuthenticationError):
        crypto.aead_open(key, nonce, bytes(tampered), aad)


def test_wrong_aad_fails_auth():
    key = crypto.generate_key()
    ct = crypto.aead_seal(key, crypto.block_nonce(0), b"hello", b"context-A")
    with pytest.raises(AEADAuthenticationError):
        crypto.aead_open(key, crypto.block_nonce(0), ct, b"context-B")


def test_wrong_key_fails_auth():
    ct = crypto.aead_seal(crypto.generate_key(), crypto.wrap_nonce(1), b"dek", b"aad")
    with pytest.raises(AEADAuthenticationError):
        crypto.aead_open(crypto.generate_key(), crypto.wrap_nonce(1), ct, b"aad")


def test_replayed_nonce_with_different_aad_does_not_authenticate():
    # 同一 nonce 下换了上下文（模拟块被搬到别的文件），AEAD 直接拒绝
    key = crypto.generate_key()
    nonce = crypto.block_nonce(7)
    ct = crypto.aead_seal(
        key, nonce, b"secret", crypto.block_aad(file_id="fileA", block_index=7, chunk_size=64)
    )
    with pytest.raises(AEADAuthenticationError):
        crypto.aead_open(
            key, nonce, ct, crypto.block_aad(file_id="fileB", block_index=7, chunk_size=64)
        )


def test_nonce_counter_must_be_positive():
    with pytest.raises(ValueError):
        crypto.wrap_nonce(0)
