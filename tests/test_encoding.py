"""Tests for versioned share encoding (compact + JSON forms)."""

import base64
import json

import pytest

from threshold_shares.encoding import (
    encode_share,
    decode_share,
    share_to_json_object,
    ShareEncodingError,
    UnsupportedVersionError,
)
from threshold_shares.scheme import split_secret, PARAM_VERSION


def _sample():
    shares = split_secret(b"format-test-secret", 3, 5)
    return shares[0]


def test_compact_roundtrip_starts_with_prefix_and_has_version_magic():
    share = _sample()
    token = encode_share(share, 3, 5)
    assert token.startswith("SSS1$")
    raw = base64.urlsafe_b64decode(token[len("SSS1$") :] + "==")
    assert raw[:4] == b"SSS1"
    decoded, threshold, total = decode_share(token)
    assert decoded == share and threshold == 3 and total == 5


def test_json_object_roundtrip():
    share = _sample()
    obj = share_to_json_object(share, 3, 5)
    assert obj["version"] == PARAM_VERSION and obj["x"] == share.x
    decoded, threshold, total = decode_share(obj)
    assert decoded == share and threshold == 3 and total == 5
    # JSON text form also accepted
    decoded2, _, _ = decode_share(json.dumps(obj))
    assert decoded2 == share


def test_roundtrip_across_block_counts():
    for secret in [b"a", b"x" * 31, b"y" * 93, b"z" * 4096]:
        shares = split_secret(secret, 2, 4)
        token = encode_share(shares[3], 2, 4)
        decoded, threshold, total = decode_share(token)
        assert decoded == shares[3] and (threshold, total) == (2, 4)


def test_corrupt_encoding_noncanonical_base64_rejected():
    token = encode_share(_sample(), 3, 5)
    with pytest.raises(ShareEncodingError):
        decode_share(token + "=")  # padding forbidden
    with pytest.raises(ShareEncodingError):
        decode_share(token + "\n")
    with pytest.raises(ShareEncodingError):
        decode_share(token[:-8])  # truncation (beyond one whole 32-byte block)
    # non-alphabet characters are never normalised away
    with pytest.raises(ShareEncodingError):
        decode_share(token[:-5] + " " + token[-4:])
    # inserting a standard-alphabet '+' is also rejected (it is not base64url)
    with pytest.raises(ShareEncodingError):
        decode_share(token[:10] + "+" + token[11:])


def test_corrupt_binary_inside_base64_rejected():
    share = _sample()
    raw = bytearray(base64.urlsafe_b64decode(encode_share(share, 3, 5)[5:] + "=="))
    # corrupt the x byte into an out-of-range value (> total=5)
    raw[5] = 50
    token = "SSS1$" + base64.urlsafe_b64encode(bytes(raw)).rstrip(b"=").decode()
    with pytest.raises(ShareEncodingError):
        decode_share(token)


def test_field_element_above_prime_rejected():
    share = _sample()
    raw = bytearray(base64.urlsafe_b64decode(encode_share(share, 3, 5)[5:] + "=="))
    # Set the first y block to all 0xFF = 2^256-1, which is >= p.
    first_y = 10
    raw[first_y : first_y + 32] = b"\xff" * 32
    token = "SSS1$" + base64.urlsafe_b64encode(bytes(raw)).rstrip(b"=").decode()
    with pytest.raises(ShareEncodingError, match="GF"):
        decode_share(token)


def test_unknown_json_version_rejected_explicitly():
    obj = share_to_json_object(_sample(), 3, 5)
    obj["version"] = 99
    with pytest.raises(UnsupportedVersionError):
        decode_share(obj)


def test_unknown_json_field_rejected():
    obj = share_to_json_object(_sample(), 3, 5)
    obj["future_extension"] = 1
    with pytest.raises(UnsupportedVersionError):
        decode_share(obj)


def test_binary_v2_magic_is_unsupported_version():
    raw = b"SSS2" + b"\x00" * 40
    token = "SSS1$" + base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    with pytest.raises(UnsupportedVersionError):
        decode_share(token)


def test_malformed_json_share_rejected():
    for bad in [
        {"version": 1},
        {"version": 1, "x": 1, "threshold": 3, "total": 5, "y": []},
        {"version": 1, "x": 0, "threshold": 3, "total": 5, "y": ["AAAA"]},
        {"version": 1, "x": 9, "threshold": 3, "total": 5, "y": ["AAAA"]},
        {"version": "1", "x": 1, "threshold": 3, "total": 5, "y": ["AAAA"]},
        "not-an-object",
    ]:
        with pytest.raises(ShareEncodingError):
            decode_share(bad)


def test_bare_bytes_and_bad_input_types():
    share = _sample()
    raw = base64.urlsafe_b64decode(encode_share(share, 3, 5)[5:] + "==")
    decoded, _, _ = decode_share(raw)
    assert decoded == share
    with pytest.raises(ShareEncodingError):
        decode_share(123)
    with pytest.raises(ShareEncodingError):
        decode_share(share)


def test_encode_rejects_inconsistent_parameters():
    share = _sample()
    with pytest.raises(ShareEncodingError):
        encode_share(share, 1, 5)  # threshold < 2
    with pytest.raises(ShareEncodingError):
        encode_share(share, 6, 5)  # threshold > total
    with pytest.raises(ShareEncodingError):
        encode_share(share, 3, 256)
