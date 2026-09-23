"""密码学原语单元测试：SMT 成员/非成员证明与 Ed25519 检查点签名。"""

from __future__ import annotations

import pytest

from ibc_teach import crypto


def test_empty_tree_root_stable():
    assert crypto.SparseMerkleTree().root() == crypto.SparseMerkleTree().root()
    assert len(crypto.SparseMerkleTree().root()) == 32


def test_membership_proofs_verify():
    items = {b"alpha": b"1", b"beta": b"22", b"": b"empty-key", b"x" * 100: b"y" * 256}
    t = crypto.SparseMerkleTree(items)
    root = t.root().hex()
    for k, v in items.items():
        steps = t.prove(k)
        assert len(steps) == 256
        assert crypto.verify_proof(root, k, v, steps)
        assert not crypto.verify_proof(root, k, v + b"!", steps)  # 值被改


def test_non_membership_proofs_verify():
    t = crypto.SparseMerkleTree({b"a": b"1"})
    root = t.root().hex()
    for absent in (b"b", b"", b"\x00\x01"):
        assert crypto.verify_proof(root, absent, None, t.prove(absent))
        # 非成员不能伪装成任何具体成员值。
        assert not crypto.verify_proof(root, absent, b"", t.prove(absent))


def test_existing_key_cannot_be_proven_absent():
    t = crypto.SparseMerkleTree({b"k": b"v"})
    root = t.root().hex()
    assert not crypto.verify_proof(root, b"k", None, t.prove(b"k"))


def test_tampered_proof_rejected():
    t = crypto.SparseMerkleTree({b"k": b"v"})
    root = t.root().hex()
    steps = t.prove(b"k")
    s = steps[42].sibling
    steps[42] = crypto.ProofStep(
        steps[42].level, steps[42].bit,
        ("00" if s[:2] != "00" else "11") + s[2:],
    )
    assert not crypto.verify_proof(root, b"k", b"v", steps)


def test_malformed_proof_rejected():
    t = crypto.SparseMerkleTree({b"k": b"v"})
    root = t.root().hex()
    steps = t.prove(b"k")
    assert not crypto.verify_proof(root, b"k", b"v", steps[:100])  # 长度不足
    bad = [crypto.ProofStep((s.level + 1) % 256, s.bit, s.sibling) for s in steps]
    assert not crypto.verify_proof(root, b"k", b"v", bad)  # 层级乱序


def test_insertion_order_does_not_change_root():
    items = {bytes([i]): bytes([i, i]) for i in range(20)}
    a = crypto.SparseMerkleTree(items)
    b = crypto.SparseMerkleTree()
    for k in reversed(list(items)):
        b.set(k, items[k])
    assert a.root() == b.root()


def test_checkpoint_signature_roundtrip_and_tamper():
    vk, sk = crypto.generate_signing_key()
    sig = crypto.sign_checkpoint(sk, "chain-a", 9, 123456, "ab" * 32, "cd" * 32)
    good = dict(chain_id="chain-a", height=9, time_ns=123456,
                app_hash="ab" * 32, previous_app_hash="cd" * 32, signature_hex=sig)
    assert crypto.verify_checkpoint_signature(vk, **good)
    # 高度 +1（旧块签名重放）
    tampered = dict(good, height=10)
    assert not crypto.verify_checkpoint_signature(vk, **tampered)
    # 时间回拨
    assert not crypto.verify_checkpoint_signature(vk, **dict(good, time_ns=1))
    # 错误验证密钥
    vk2, _ = crypto.generate_signing_key()
    assert not crypto.verify_checkpoint_signature(vk2, **good)
    # 签名尾部损坏（保证翻转：原结尾若是 00 就改成 11）
    flipped = ("00" if sig[-2:] != "00" else "11")
    assert not crypto.verify_checkpoint_signature(
        vk, **dict(good, signature_hex=sig[:-2] + flipped)
    )
    # 非法 hex 安全返回 False，不抛异常
    assert not crypto.verify_checkpoint_signature(
        vk, **dict(good, signature_hex="zz")
    )


@pytest.mark.parametrize("n", [0, 1, 2, 5, 50])
def test_roots_differ_with_n_leaves(n):
    items = {f"key-{i}".encode(): f"val-{i}".encode() for i in range(n)}
    root = crypto.SparseMerkleTree(items).root()
    other = crypto.SparseMerkleTree({b"different": b"tree"}).root()
    if items:
        assert root != other
