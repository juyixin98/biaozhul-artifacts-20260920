"""Tests for cryptographic primitives against published test vectors."""

import hashlib

import pytest

from provenance_service.crypto import (
    base58btc_decode,
    base58btc_encode,
    canonical_json,
    chain_hash,
    eip55_checksum,
    generate_private_key,
    ipfs_cid_v0,
    json_digest,
    keccak256,
    public_key_hex,
    sign_message,
    verify_eip55,
    verify_signature,
)


def test_keccak_known_vectors():
    assert keccak256(b"").hex() == (
        "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470")
    assert keccak256(b"abc").hex() == (
        "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45")
    # Differs from NIST SHA3-256 (padding) — proves it is real Keccak.
    assert keccak256(b"abc").hex() != hashlib.sha3_256(b"abc").hexdigest()


def test_keccak_avalanche_single_byte():
    a = keccak256(b"same").hex()
    b = keccak256(b"sAme").hex()
    assert a != b


@pytest.mark.parametrize("addr", [
    "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed",
    "0xfB6916095ca1df60bB79Ce92cE3Ea74c37c5d359",
    "0xdbF03B407c01E7cD3CBea99509d93f8DDDC8C6FB",
    "0xD1220A0cf47c7B9Be7A2E6BA89F429762e7b9aDb",
])
def test_eip55_official_vectors(addr):
    assert eip55_checksum(addr.lower()) == addr
    assert verify_eip55(addr)


def test_eip55_rejects_lowercase_and_flipped_nibble():
    good = "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"
    assert not verify_eip55(good.lower())
    assert not verify_eip55(good[:-1] + ("d" if good[-1] != "d" else "e"))
    assert not verify_eip55("0x1234")


def test_base58btc_known_and_roundtrip():
    assert base58btc_encode(b"hello world") == "StV1DL6CwTryKyV"
    for blob in [b"", b"\x00", b"\x00\x00abc", bytes(range(256))]:
        enc = base58btc_encode(blob)
        assert base58btc_decode(enc) == blob


def test_cid_v0_empty_block_well_known():
    cid = ipfs_cid_v0(hashlib.sha256(b"").digest())
    assert cid == "QmdfTbBqBPQ7VNxZEYEj14VmRuZBkqFbiwReogJgS1zR1n"
    raw = base58btc_decode(cid)
    assert raw[:2] == bytes([0x12, 0x20])  # sha2-256, 32-byte length


def test_canonical_json_is_order_independent():
    a = canonical_json({"b": 1, "a": [1, 2, {"x": 1, "y": 2}]})
    b = canonical_json({"a": [1, 2, {"y": 2, "x": 1}], "b": 1})
    assert a == b
    assert json_digest({"a": 1}) == json_digest({"a": 1})


def test_chain_hash_anchors_and_links():
    assert chain_hash(None, json_digest({"x": 1})) == chain_hash(None, json_digest({"x": 1}))
    h1 = chain_hash(None, json_digest({"x": 1}))
    h2 = chain_hash(h1, json_digest({"x": 2}))
    assert h1 != h2
    assert h2 == chain_hash(h1, json_digest({"x": 2}))


def test_ed25519_signature_real_verification():
    key = generate_private_key()
    msg = canonical_json({"job": "abc", "digest": "00" * 32})
    sig = sign_message(key, msg)
    pub = public_key_hex(key)
    assert verify_signature(pub, msg, sig)
    assert not verify_signature(pub, msg + b" ", sig)
    other = generate_private_key()
    assert not verify_signature(public_key_hex(other), msg, sig)
