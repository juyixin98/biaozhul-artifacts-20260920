"""Unit tests for the cryptographic layer."""

import pytest

from enrange import crypto
from cryptography.exceptions import InvalidTag


def test_master_key_is_256_bits():
    key = crypto.generate_master_key()
    assert len(key) == 32
    assert key != crypto.generate_master_key()  # random


def test_object_keys_are_distinct_and_bound_to_id():
    master = crypto.generate_master_key()
    k1 = crypto.derive_object_key(master, b"a" * 16)
    k2 = crypto.derive_object_key(master, b"b" * 16)
    k3 = crypto.derive_object_key(crypto.generate_master_key(), b"a" * 16)
    assert k1 != k2
    assert k1 != k3
    assert len(k1) == 32


def test_block_nonces_are_unique_and_deterministic():
    n0 = crypto.block_nonce(0)
    n1 = crypto.block_nonce(1)
    assert len(n0) == 12
    assert n0 != n1
    assert n0 == crypto.block_nonce(0)
    assert n0[:4] != crypto._HEADER_NONCE[:4]


def test_aead_roundtrip_detects_ciphertext_flip():
    key = crypto.derive_object_key(crypto.generate_master_key(), b"id")
    aad = b"context"
    ct = crypto.seal_block(key, 7, b"plain block", aad)
    assert crypto.open_block(key, 7, ct, aad) == b"plain block"

    tampered = bytearray(ct)
    tampered[0] ^= 0x01
    with pytest.raises(InvalidTag):
        crypto.open_block(key, 7, bytes(tampered), aad)


def test_aad_change_is_detected():
    key = crypto.derive_object_key(crypto.generate_master_key(), b"id")
    ct = crypto.seal_block(key, 0, b"x", b"aad-one")
    with pytest.raises(InvalidTag):
        crypto.open_block(key, 0, ct, b"aad-two")


def test_nonce_reuse_position_is_part_of_identity():
    # Same key, same nonce (position 0), but different AAD must fail:
    # swapping blocks across positions cannot authenticate.
    key = crypto.derive_object_key(crypto.generate_master_key(), b"id")
    ct = crypto.seal_block(key, 0, b"data", crypto.block_aad(b"id", 16, 0, 0, 4, 4, 1))
    wrong_aad = crypto.block_aad(b"id", 16, 1, 16, 4, 20, 2)
    with pytest.raises(InvalidTag):
        crypto.open_block(key, 1, ct, wrong_aad)


def test_header_seal_and_open():
    key = crypto.derive_object_key(crypto.generate_master_key(), b"id")
    aad = crypto.header_aad(b"id", 16, 0, 0)
    tag = crypto.seal_header(key, crypto.HEADER_PAYLOAD, aad)
    assert len(tag) == 16
    assert crypto.open_header(key, tag, aad) == b""
    bad_aad = crypto.header_aad(b"id", 16, 1, 17)
    with pytest.raises(InvalidTag):
        crypto.open_header(key, tag, bad_aad)
