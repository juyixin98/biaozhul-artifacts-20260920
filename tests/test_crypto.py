"""Unit tests for the envelope-encryption core."""

import struct

import pytest

from app import crypto
from app.crypto import EnvelopeError


def make_store(*keks):
    keys = {i + 1: k for i, k in enumerate(keks)}
    return keys, keys.__getitem__


def test_roundtrip():
    kek = crypto.generate_kek()
    _, get = make_store(kek)
    blob = crypto.encrypt(kek, 1, b"hello envelope")
    assert crypto.decrypt(get, blob) == b"hello envelope"


def test_empty_plaintext_roundtrip():
    kek = crypto.generate_kek()
    _, get = make_store(kek)
    blob = crypto.encrypt(kek, 1, b"")
    assert crypto.decrypt(get, blob) == b""


def test_unique_nonce_and_dek_per_object():
    kek = crypto.generate_kek()
    a = crypto.encrypt(kek, 1, b"same plaintext")
    b = crypto.encrypt(kek, 1, b"same plaintext")
    assert a != b
    # data nonces (bytes 68..80) differ -> unique nonce per object
    assert a[68:80] != b[68:80]
    # wrapped DEKs differ -> independent data key per object
    assert a[20:68] != b[20:68]


def test_wrong_key_fails_without_plaintext():
    kek = crypto.generate_kek()
    other = crypto.generate_kek()
    _, get_other = make_store(other)
    blob = crypto.encrypt(kek, 1, b"secret")
    with pytest.raises(EnvelopeError):
        crypto.decrypt(get_other, blob)


def test_unknown_kek_version_fails():
    kek = crypto.generate_kek()
    _, get = make_store(kek)
    blob = crypto.encrypt(kek, 5, b"secret")  # store only has version 1
    with pytest.raises(EnvelopeError):
        crypto.decrypt(get, blob)


@pytest.mark.parametrize(
    "offset",
    [0, 5, 10, 30, 70],  # magic, kek_version, wrap_nonce, wrapped_dek, data_nonce
)
def test_header_tamper_fails(offset):
    kek = crypto.generate_kek()
    _, get = make_store(kek)
    blob = bytearray(crypto.encrypt(kek, 1, b"tamper me"))
    blob[offset] ^= 0x01
    with pytest.raises(EnvelopeError):
        crypto.decrypt(get, bytes(blob))


def test_kek_version_tamper_fails():
    """Flipping the header's key version must be caught by the wrap AAD."""
    kek1, kek2 = crypto.generate_kek(), crypto.generate_kek()
    _, get = make_store(kek1, kek2)  # both versions exist in the store
    blob = bytearray(crypto.encrypt(kek1, 1, b"version tamper"))
    blob[4:8] = struct.pack(">I", 2)  # claim it was wrapped with v2
    with pytest.raises(EnvelopeError):
        crypto.decrypt(get, bytes(blob))


def test_ciphertext_tamper_fails():
    kek = crypto.generate_kek()
    _, get = make_store(kek)
    blob = bytearray(crypto.encrypt(kek, 1, b"authenticated data"))
    blob[-1] ^= 0x01
    with pytest.raises(EnvelopeError):
        crypto.decrypt(get, bytes(blob))


@pytest.mark.parametrize("cut", [1, 8, 16, 40, 79, 80, 81])
def test_truncation_fails(cut):
    kek = crypto.generate_kek()
    _, get = make_store(kek)
    blob = crypto.encrypt(kek, 1, b"x" * 100)
    with pytest.raises(EnvelopeError):
        crypto.decrypt(get, blob[: len(blob) - cut])


def test_rewrap_rotation_only_touches_header():
    kek1 = crypto.generate_kek()
    store, get = make_store(kek1)
    blob = crypto.encrypt(kek1, 1, b"rotate me")

    kek2 = crypto.generate_kek()
    store[2] = kek2
    new_blob = crypto.rewrap(get, kek2, 2, blob)

    # data ciphertext and data nonce are untouched by the rotation
    assert new_blob[68:] == blob[68:]
    assert new_blob[:80] != blob[:80]
    # decrypts fine under the store holding both versions
    assert crypto.decrypt(get, new_blob) == b"rotate me"
    # and the pre-rotation object still decrypts (old key retained)
    assert crypto.decrypt(get, blob) == b"rotate me"


def test_interrupted_rotation_leaves_everything_decryptable():
    """Simulate a crash mid-rotation: some objects re-wrapped, some not."""
    kek1 = crypto.generate_kek()
    store, get = make_store(kek1)
    plaintexts = [f"object-{i}".encode() for i in range(5)]
    blobs = [crypto.encrypt(kek1, 1, p) for p in plaintexts]

    kek2 = crypto.generate_kek()
    store[2] = kek2

    rotated = []
    for i, blob in enumerate(blobs):
        if i == 3:
            break  # crash here: objects 0-2 re-wrapped, 3-4 not
        rotated.append(crypto.rewrap(get, kek2, 2, blob))
    rotated.extend(blobs[len(rotated):])

    for blob, expected in zip(rotated, plaintexts):
        assert crypto.decrypt(get, blob) == expected


def test_rewrap_rejects_tampered_object():
    kek1 = crypto.generate_kek()
    store, get = make_store(kek1)
    blob = bytearray(crypto.encrypt(kek1, 1, b"integrity"))
    blob[30] ^= 0x01  # corrupt the wrapped DEK
    kek2 = crypto.generate_kek()
    with pytest.raises(EnvelopeError):
        crypto.rewrap(get, kek2, 2, bytes(blob))
