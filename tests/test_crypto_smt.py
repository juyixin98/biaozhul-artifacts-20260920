# -*- coding: utf-8 -*-
"""密码学与状态树根的单元测试（真实 SHA-256 / Ed25519 / SMT 证明）。"""
from __future__ import annotations

import os

import pytest

from app import crypto
from app.encoding import canonical_json, u64be
from app.smt import DEPTH, MemoryNodeStore, SparseMerkleTree


def test_ed25519_real_signature_roundtrip():
    sk, pk = crypto.generate_keypair()
    msg = b"checkpoint-height-42"
    sig = crypto.sign(sk, msg)
    assert len(sig) == 64
    assert crypto.verify_signature(pk, sig, msg) is True
    # 篡改消息 / 签名 / 公钥任一都必须失败
    assert crypto.verify_signature(pk, sig, msg + b"x") is False
    assert crypto.verify_signature(pk, sig[:-1] + bytes([sig[-1] ^ 1]), msg) is False
    sk2, pk2 = crypto.generate_keypair()
    assert crypto.verify_signature(pk2, sig, msg) is False


def test_packet_commitment_matches_ibc_style_encoding():
    data = b"transfer/10"
    c1 = crypto.packet_commitment(1, 7, 123456789, data)
    assert len(c1) == 32
    # 手工按协议字节序组装应得到同一承诺
    from app.crypto import sha256
    raw = u64be(1) + u64be(7) + u64be(123456789) + sha256(data)
    assert sha256(raw) == c1
    # 任一字段变化都改变承诺
    assert crypto.packet_commitment(1, 8, 123456789, data) != c1
    assert crypto.packet_commitment(1, 7, 123456790, data) != c1
    assert crypto.packet_commitment(1, 7, 123456789, data + b"!") != c1


def test_ack_commitment_double_sha():
    assert crypto.ack_commitment(b"success") == crypto.sha256(crypto.sha256(b"success"))


def test_smt_membership_and_non_membership():
    t = SparseMerkleTree(MemoryNodeStore())
    k1, v1 = b"key-alpha", b"v1"
    root = t.set(k1, v1)
    p = t.prove(k1)
    assert SparseMerkleTree.verify(root, p["key"], p["value"], p["siblings"]) == root
    # 不存在键：非成员证明
    missing = b"key-missing"
    pn = t.prove(missing)
    assert pn["membership"] is False
    assert SparseMerkleTree.verify(root, pn["key"], b"", pn["siblings"]) == root
    # 伪造值/伪造兄弟均失败
    assert SparseMerkleTree.verify(root, p["key"], b"v2", p["siblings"]) != root
    bad = list(p["siblings"])
    bad[0] = bytes(32)
    assert SparseMerkleTree.verify(root, p["key"], p["value"], bad) != root


def test_smt_many_keys_and_delete_returns_to_empty_root():
    t = SparseMerkleTree(MemoryNodeStore())
    empty = t.root()
    keys = [os.urandom(12) for _ in range(60)]
    for k in keys:
        t.set(k, crypto.sha256(k))
    root = t.root()
    for k in keys:
        p = t.prove(k)
        assert SparseMerkleTree.verify(root, p["key"], p["value"], p["siblings"]) == root
    # 一半键删除后非成员证明成立，其余成员证明仍成立
    for k in keys[:30]:
        t.delete(k)
    root2 = t.root()
    for k in keys[:30]:
        p = t.prove(k)
        assert p["membership"] is False
        assert SparseMerkleTree.verify(root2, p["key"], b"", p["siblings"]) == root2
    for k in keys[30:]:
        p = t.prove(k)
        assert SparseMerkleTree.verify(root2, p["key"], p["value"], p["siblings"]) == root2
    for k in keys[30:]:
        t.delete(k)
    assert t.root() == empty


def test_smt_two_stores_agree_on_proofs():
    """同一份键值在两个独立存储上产生的根与证明可交叉验证。"""
    items = {b"a": b"1", b"bb": b"22", b"ccc": b"333", b"d" * 32: b"four"}
    t1 = SparseMerkleTree(MemoryNodeStore())
    t2 = SparseMerkleTree(MemoryNodeStore())
    for k, v in items.items():
        t1.set(k, v)
        t2.set(k, v)
    assert t1.root() == t2.root()
    for k, v in items.items():
        p1 = t1.prove(k)
        assert SparseMerkleTree.verify(t2.root(), p1["key"], p1["value"], p1["siblings"]) == t2.root()


def test_canonical_json_is_deterministic():
    a = canonical_json({"z": 1, "a": [1, 2, {"y": 2, "x": 1}]})
    b = canonical_json({"a": [1, 2, {"x": 1, "y": 2}], "z": 1})
    assert a == b
