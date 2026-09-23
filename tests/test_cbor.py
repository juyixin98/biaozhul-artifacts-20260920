"""Tests for the CBOR codec and Solidity metadata-tail handling."""

import pytest

from provenance_service import cbor
from provenance_service.cbor import (
    CBORError,
    build_metadata_tail,
    cbor_decode,
    cbor_encode,
    parse_metadata_tail,
)


@pytest.mark.parametrize("value", [
    0, 1, 23, 24, 100, 65535, 65536, 2 ** 40,
    b"", b"\x00\x01\x02", b"x" * 100,
    "", "ipfs", "keccak256", "x" * 100,
    True, False, None,
    [], [1, 2, 3], ["a", b"b", 3, True],
    {}, {"ipfs": b"0" * 32, "solc": bytes([0, 8, 24])},
    {"nested": {"a": [1, 2], "b": b"z"}, "n": 256},
])
def test_cbor_roundtrip(value):
    assert cbor_decode(cbor_encode(value)) == value


def test_cbor_rejects_trailing_bytes():
    enc = cbor_encode(1) + cbor_encode(2)
    with pytest.raises(CBORError):
        cbor_decode(enc)


def test_metadata_tail_roundtrip_and_fields():
    meta = {"ipfs": bytes(range(32)), "solc": bytes([0, 8, 24])}
    tail = build_metadata_tail(meta).hex()
    parsed = parse_metadata_tail("6080" + tail)
    assert parsed["cbor_map"]["ipfs"] == bytes(range(32))
    assert parsed["cbor_map"]["solc"] == bytes([0, 8, 24])
    assert parsed["body_hex"] == "6080"
    assert parsed["length_field"] == len(cbor_encode(meta))


def test_metadata_tail_missing_sentinel():
    with pytest.raises(CBORError):
        parse_metadata_tail("6080" + "aabbccdd")


def test_metadata_tail_bad_length_field():
    # length claims 600 bytes but blob is tiny
    blob = "a1" + "0033"
    with pytest.raises(CBORError):
        parse_metadata_tail(blob)


def test_metadata_tail_corrupt_cbor():
    good = build_metadata_tail({"solc": bytes([0, 8, 24])}).hex()
    # flip a payload nibble -> CBOR should fail rather than be guessed
    bad = ("f" if good[0] != "f" else "e") + good[1:]
    with pytest.raises(CBORError):
        parse_metadata_tail(bad)
