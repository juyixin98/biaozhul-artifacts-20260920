"""密码学原语测试：真实 Ed25519 签名/验证，域分离消息编码。"""
from __future__ import annotations

from app import crypto

from .conftest import BLOCK_A, BLOCK_B


def test_sign_and_verify_roundtrip():
    sk = crypto.derive_private_key(b"test-key-1")
    pub = crypto.public_key_hex(sk)
    sig = crypto.sign_vote(sk, 5, BLOCK_A)
    assert crypto.verify_vote(pub, 5, BLOCK_A, sig)


def test_verify_rejects_wrong_block_epoch_and_key():
    sk = crypto.derive_private_key(b"test-key-2")
    other = crypto.derive_private_key(b"test-key-3")
    pub = crypto.public_key_hex(sk)
    sig = crypto.sign_vote(sk, 5, BLOCK_A)
    assert not crypto.verify_vote(pub, 5, BLOCK_B, sig)      # 块不同
    assert not crypto.verify_vote(pub, 6, BLOCK_A, sig)      # epoch 不同
    assert not crypto.verify_vote(crypto.public_key_hex(other), 5, BLOCK_A, sig)  # 钥不同


def test_verify_rejects_malformed_inputs():
    sk = crypto.derive_private_key(b"test-key-4")
    pub = crypto.public_key_hex(sk)
    assert not crypto.verify_vote(pub, 5, BLOCK_A, "zz" * 64)   # 非 hex
    assert not crypto.verify_vote(pub, 5, BLOCK_A, "ab" * 32)   # 长度错
    assert not crypto.verify_vote("not-a-key", 5, BLOCK_A, "ab" * 64)


def test_message_encoding_is_unambiguous():
    # 长度前缀编码：不同 (epoch, block) 不会拼接出相同消息
    m1 = crypto.vote_message(1, "ab" * 32)
    m2 = crypto.vote_message(11, "b" * 64)
    assert m1 != m2
    assert crypto.vote_message(0, BLOCK_A) != crypto.vote_message(1, BLOCK_A)
