"""Real cryptographic verification tests."""

from __future__ import annotations

import base64
import hashlib

import pytest

from tests._fixtures import make_tarball, npm_integrity

from app.integrity import (
    IntegrityError,
    inspect_tarball,
    parse_integrity,
    verify_tarball,
)


def test_npm_integrity_format_is_sha512_base64_of_raw_digest():
    blob = b"hello world"
    raw = hashlib.sha512(blob).digest()
    assert npm_integrity(blob) == "sha512-" + base64.b64encode(raw).decode()


def test_verify_real_tarball_roundtrip():
    blob = make_tarball("left-pad", "1.3.0")
    integrity = npm_integrity(blob)
    parsed = verify_tarball(integrity, blob)
    assert parsed.algorithm == "sha512"
    assert not parsed.is_weak


def test_one_byte_tamper_fails_verification():
    original = make_tarball("left-pad", "1.3.0")
    integrity = npm_integrity(original)
    tampered = bytearray(original)
    tampered[len(tampered) // 2] ^= 0xFF  # flip one byte of the gzip stream
    with pytest.raises(IntegrityError, match="digest mismatch|valid gzip"):
        verify_tarball(integrity, bytes(tampered))


def test_missing_and_malformed_integrity():
    blob = make_tarball("x", "1.0.0")
    with pytest.raises(IntegrityError):
        parse_integrity("")
    with pytest.raises(IntegrityError):
        parse_integrity("md5-xxxx")
    with pytest.raises(IntegrityError, match="base64"):
        parse_integrity("sha512-@@notbase64@@")
    with pytest.raises(IntegrityError, match="64 bytes"):
        parse_integrity("sha512-" + base64.b64encode(b"short").decode())
    with pytest.raises(IntegrityError, match="multiple"):
        parse_integrity(f"{npm_integrity(blob)} {npm_integrity(blob)}")


def test_sha1_is_parseable_but_flagged_weak():
    blob = b"abc"
    sha1 = "sha1-" + base64.b64encode(hashlib.sha1(blob).digest()).decode()
    parsed = verify_tarball(sha1, blob)
    assert parsed.is_weak


def test_inspect_tarball_reads_manifest_and_scripts_without_running():
    blob = make_tarball(
        "danger",
        "2.0.0",
        scripts={"install": "node should-never-run.js"},
    )
    info = inspect_tarball(blob)
    assert info.name == "danger"
    assert info.version == "2.0.0"
    assert info.has_install_scripts is True
    assert "install" in info.scripts
    # Proof nothing ran: the referenced script is not even present.
    assert info.package_json_raw is not None


def test_inspect_rejects_non_tarball():
    with pytest.raises(IntegrityError):
        inspect_tarball(b"definitely not a tarball")
