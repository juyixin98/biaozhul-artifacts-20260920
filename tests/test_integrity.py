"""Real cryptographic verification tests."""
from __future__ import annotations

import base64
import hashlib

import pytest

from app.lockgate.integrity import IntegrityError, compute_digest, parse_integrity, verify


def test_roundtrip_sha512():
    data = b"real artifact bytes\n" * 50
    digest = hashlib.sha512(data).digest()
    sri = "sha512-" + base64.b64encode(digest).decode()
    integ = parse_integrity(sri)
    assert integ.algorithm == "sha512"
    assert verify(data, integ)


@pytest.mark.parametrize("alg", ["sha256", "sha384", "sha512"])
def test_each_algorithm(alg):
    data = b"abc"
    integ = parse_integrity(f"{alg}-" + base64.b64encode(compute_digest(data, alg)).decode())
    assert verify(data, integ)
    assert not verify(data + b"x", integ)


def test_single_byte_change_fails():
    data = b"A" * 4096
    digest = hashlib.sha512(data).digest()
    integ = parse_integrity("sha512-" + base64.b64encode(digest).decode())
    tampered = bytearray(data)
    tampered[17] ^= 0x01
    assert not verify(bytes(tampered), integ)


@pytest.mark.parametrize("bad", [
    None, "", "   ",
    "md5-abc",
    "sha512-!!!notbase64",
    "sha256-" + base64.b64encode(b"short").decode(),
    "sha512-AAAA BBBB",  # multiple hashes in one field
])
def test_malformed_integrity(bad):
    with pytest.raises(IntegrityError):
        parse_integrity(bad)


def test_digest_length_enforced():
    # valid base64 but wrong length for the declared algorithm
    wrong = base64.b64encode(b"0" * 16).decode()
    with pytest.raises(IntegrityError):
        parse_integrity(f"sha512-{wrong}")
