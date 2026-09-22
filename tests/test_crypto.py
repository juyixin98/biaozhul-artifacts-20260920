"""密码学与规范化基础测试：真实 Ed25519、证据排序规范化、快照哈希。"""
from __future__ import annotations

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.crypto import (
    canonical_evidence_blob,
    content_hash,
    derive_pubkey,
    evidence_id,
    sha256,
    sign_vote,
    snapshot_canonical,
    verify_signature,
    vote_signed_bytes,
)


def _key(seed: int = 1):
    return Ed25519PrivateKey.from_private_bytes(bytes([seed]) * 32)


def test_real_ed25519_signature_roundtrip():
    priv = _key(1)
    pub = derive_pubkey(priv)
    msg = vote_signed_bytes(
        chain_id="c1", validator_pubkey=pub, round=3, block_hash=b"\x01" * 32
    )
    sig = sign_vote(priv, chain_id="c1", validator_pubkey=pub, round=3,
                    block_hash=b"\x01" * 32)
    assert len(sig) == 64
    assert verify_signature(pub, sig, msg) is True


def test_bad_signature_rejected():
    priv = _key(1)
    other = _key(2)
    pub = derive_pubkey(priv)
    msg = vote_signed_bytes(chain_id="c1", validator_pubkey=pub, round=3,
                            block_hash=b"\x01" * 32)
    forged = sign_vote(other, chain_id="c1", validator_pubkey=pub, round=3,
                       block_hash=b"\x01" * 32)
    assert verify_signature(pub, forged, msg) is False
    # 篡改一个位
    sig = bytearray(sign_vote(priv, chain_id="c1", validator_pubkey=pub, round=3,
                              block_hash=b"\x01" * 32))
    sig[0] ^= 0x01
    assert verify_signature(pub, bytes(sig), msg) is False
    # 畸形公钥/签名
    assert verify_signature(b"\x00" * 31, b"\x00" * 64, msg) is False
    assert verify_signature(pub, b"\x00" * 63, msg) is False


def test_chain_id_bound_inside_signature():
    """同一轮同一区块，chain_id 不同 -> 被签字节不同，签名不能跨链复用。"""
    priv = _key(1)
    pub = derive_pubkey(priv)
    bh = b"\xab" * 32
    sig_a = sign_vote(priv, chain_id="chain-A", validator_pubkey=pub, round=5,
                      block_hash=bh)
    msg_b = vote_signed_bytes(chain_id="chain-B", validator_pubkey=pub, round=5,
                              block_hash=bh)
    assert verify_signature(pub, sig_a, msg_b) is False


def test_identical_votes_same_content_hash():
    priv = _key(1)
    pub = derive_pubkey(priv)
    kw = dict(chain_id="c1", validator_pubkey=pub, round=7, block_hash=b"\x02" * 32)
    h1 = content_hash(**kw)
    h2 = content_hash(**kw)
    assert h1 == h2 == sha256(vote_signed_bytes(**kw))


def test_different_blocks_different_content_hash():
    priv = _key(1)
    pub = derive_pubkey(priv)
    h_a = content_hash(chain_id="c1", validator_pubkey=pub, round=7,
                       block_hash=b"\x0a" * 32)
    h_b = content_hash(chain_id="c1", validator_pubkey=pub, round=7,
                       block_hash=b"\x0b" * 32)
    assert h_a != h_b


def test_canonical_blob_order_independent():
    va = vote_signed_bytes(chain_id="c", validator_pubkey=b"pk1", round=1,
                           block_hash=b"\x01" * 4)
    vb = vote_signed_bytes(chain_id="c", validator_pubkey=b"pk1", round=1,
                           block_hash=b"\x02" * 4)
    assert canonical_evidence_blob([va, vb]) == canonical_evidence_blob([vb, va])
    assert evidence_id([va, vb]) == evidence_id([vb, va])
    # 不同的票对 -> 不同 ID
    vc = vote_signed_bytes(chain_id="c", validator_pubkey=b"pk1", round=1,
                           block_hash=b"\x03" * 4)
    assert evidence_id([va, vb]) != evidence_id([va, vc])


def test_snapshot_canonical_sorted_and_stable():
    e1 = [(b"b", 20), (b"a", 10)]
    e2 = [(b"a", 10), (b"b", 20)]
    assert snapshot_canonical(e1) == snapshot_canonical(e2)
    assert snapshot_canonical([(b"a", 10)]) != snapshot_canonical([(b"a", 11)])
