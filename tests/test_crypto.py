"""底层原语测试：AEAD、nonce 唯一性、AAD 绑定。"""

from __future__ import annotations

import pytest

from envelope_enc.crypto import (
    NONCE_PREFIX_LEN,
    InvalidTag,
    chunk_aad,
    chunk_nonce,
    open_sealed,
    random_key,
    random_nonce,
    random_nonce_prefix,
    seal,
    wrap_aad,
)


def test_seal_open_roundtrip():
    key = random_key()
    nonce = random_nonce()
    aad = b"context"
    ct = seal(key, nonce, b"hello", aad)
    assert open_sealed(key, nonce, ct, aad) == b"hello"
    # 密文 = 明文长度 + 16 字节标签
    assert len(ct) == len(b"hello") + 16


def test_wrong_key_fails():
    with pytest.raises(InvalidTag):
        open_sealed(random_key(), random_nonce(), seal(random_key(), random_nonce(), b"x", b""), b"")


def test_aad_mismatch_fails():
    key, nonce = random_key(), random_nonce()
    ct = seal(key, nonce, b"x", b"context-A")
    with pytest.raises(InvalidTag):
        open_sealed(key, nonce, ct, b"context-B")


def test_gcm_is_deterministic_under_same_key_nonce_aad():
    """固定底层行为认知：相同 key/nonce/AAD 下 GCM 输出确定，因此 nonce 绝不能复用。

    本项目用"每文件独立 DEK + 每文件随机前缀 + 块计数器"来保证 nonce 唯一。
    """
    key, nonce = random_key(), random_nonce()
    ct1 = seal(key, nonce, b"same plaintext", b"a")
    ct2 = seal(key, nonce, b"same plaintext", b"a")
    assert ct1 == ct2


def test_chunk_nonce_format_and_uniqueness_within_file():
    prefix = random_nonce_prefix()
    assert len(prefix) == NONCE_PREFIX_LEN
    nonces = [chunk_nonce(prefix, i) for i in range(1000)]
    assert len(set(nonces)) == 1000
    # 4 字节随机前缀 + 8 字节大端计数器
    assert nonces[0] == prefix + (0).to_bytes(8, "big")
    assert nonces[1] == prefix + (1).to_bytes(8, "big")


def test_chunk_nonce_index_out_of_range():
    prefix = random_nonce_prefix()
    with pytest.raises(Exception):
        chunk_nonce(prefix, -1)
    with pytest.raises(Exception):
        chunk_nonce(prefix, 1 << 32)


def test_aad_contexts_are_distinct_and_bind_fields():
    # 不同用途标签必须不同，杜绝跨上下文重放
    assert chunk_aad(file_id="f1", chunk_index=0, plaintext_len=5) != wrap_aad("mk-1")
    # 块 AAD：file_id / index / 长度任一不同则 AAD 不同
    base = chunk_aad(file_id="f1", chunk_index=0, plaintext_len=5)
    assert chunk_aad(file_id="f2", chunk_index=0, plaintext_len=5) != base
    assert chunk_aad(file_id="f1", chunk_index=1, plaintext_len=5) != base
    assert chunk_aad(file_id="f1", chunk_index=0, plaintext_len=6) != base
    # 包裹 AAD 绑定 kid
    assert wrap_aad("mk-1") != wrap_aad("mk-2")
