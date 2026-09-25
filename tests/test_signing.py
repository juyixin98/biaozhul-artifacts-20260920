"""Tests for Ed25519 STH signing with domain separation."""

import os
import stat

import pytest

from tl.signing import (
    KeyManager,
    encode_sth,
    verify_sth_signature,
    STH_CONTEXT,
)


def test_key_generation_is_local_and_persisted(tmp_path):
    kp = str(tmp_path / "k.bin")
    km = KeyManager(kp)
    assert len(km.public_key_raw()) == 32
    mode = stat.S_IMODE(os.stat(kp).st_mode)
    assert mode == 0o600  # owner-only
    km2 = KeyManager(kp)
    assert km2.public_key_raw() == km.public_key_raw()  # reloaded same key


def test_sth_signature_roundtrip(tmp_path):
    km = KeyManager(str(tmp_path / "k.bin"))
    root = b"\xab" * 32
    sth = km.sign_sth(7, root, 1_700_000_000_000_000)
    ok, reason = verify_sth_signature(
        km.public_key_raw(), 7, root, sth.timestamp_us, sth.signature
    )
    assert ok, reason


def test_signature_rejects_tampered_fields(tmp_path):
    km = KeyManager(str(tmp_path / "k.bin"))
    root = b"\xab" * 32
    ts = 1_700_000_000_000_000
    sth = km.sign_sth(7, root, ts)

    # forged tree size
    ok, _ = verify_sth_signature(km.public_key_raw(), 8, root, ts, sth.signature)
    assert not ok
    # forged root
    ok, _ = verify_sth_signature(
        km.public_key_raw(), 7, b"\xcd" * 32, ts, sth.signature
    )
    assert not ok
    # replay with different timestamp
    ok, _ = verify_sth_signature(
        km.public_key_raw(), 7, root, ts + 1, sth.signature
    )
    assert not ok
    # flipped signature byte
    sig = sth.signature
    bad_sig = bytes([sig[0] ^ 1]) + sig[1:]
    ok, _ = verify_sth_signature(km.public_key_raw(), 7, root, ts, bad_sig)
    assert not ok
    # wrong key
    other = KeyManager(str(tmp_path / "other.bin"))
    ok, _ = verify_sth_signature(
        other.public_key_raw(), 7, root, ts, sth.signature
    )
    assert not ok


def test_domain_separated_context():
    msg = encode_sth(1, b"\x00" * 32, 10)
    assert msg.startswith(STH_CONTEXT)
    assert STH_CONTEXT == b"TL-STH-v1"


def test_bad_inputs_return_false(tmp_path):
    km = KeyManager(str(tmp_path / "k.bin"))
    ok, reason = verify_sth_signature(b"short", 1, b"\x00" * 32, 1, b"sig")
    assert not ok
    with pytest.raises(ValueError):
        encode_sth(1, b"short-root", 1)
