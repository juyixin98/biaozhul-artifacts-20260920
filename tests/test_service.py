"""Tests for the fail-closed service layer -- mirrors the acceptance criteria."""

import base64
import itertools
import json

import pytest

from threshold_shares import service
from threshold_shares.sealing import seal_share, unseal_share, SealingError

from conftest import flip_last_y


def _split(secret=b"acceptance-secret", k=3, n=5):
    result = service.split(secret, k, n)
    return secret, result


# ---------------------------------------------------------------- acceptance 1

def test_any_threshold_subset_recovers():
    secret, result = _split(k=3, n=5)
    shares = result["shares"]
    for combo in itertools.combinations(range(5), 3):
        out = service.recover_from_encoded([shares[i] for i in combo])
        assert base64.b64decode(out["secret_b64"]) == secret
        assert out["used_indices"] == sorted(i + 1 for i in combo)
        assert out["secret_fingerprint"] == result["secret_fingerprint"]


def test_recovery_with_more_than_threshold_uses_exactly_k():
    secret, result = _split(k=3, n=5)
    out = service.recover_from_encoded(result["shares"][:4])
    assert base64.b64decode(out["secret_b64"]) == secret
    assert len(out["used_positions"]) == 3
    assert out["unused_positions"] == [3]
    assert out["valid_share_count"] == 4


def test_fingerprint_match_flag_on_success():
    secret, result = _split()
    out = service.recover_from_encoded(
        result["shares"][:3], expected_fingerprint=result["secret_fingerprint"]
    )
    assert out["fingerprint_matches"] is True


# ---------------------------------------------------------------- acceptance 2

def test_fewer_than_threshold_shares_refused():
    _, result = _split(k=3, n=5)
    with pytest.raises(service.InsufficientSharesError) as exc:
        service.recover_from_encoded(result["shares"][:2])
    assert exc.value.details == {"required": 3, "received": 2, "missing": 1, "total": 5}
    # Explicitly: no secret attached to the failure.
    assert not hasattr(exc.value, "secret_b64")


def test_single_and_empty_share_lists_refused():
    with pytest.raises(service.ParameterError):
        service.recover_from_encoded([])
    _, result = _split(k=3, n=5)
    with pytest.raises(service.InsufficientSharesError):
        service.recover_from_encoded(result["shares"][:1])


# ---------------------------------------------------------------- acceptance 3

def test_mixed_batch_different_threshold_rejected():
    _, a = _split(b"batch A", 3, 5)
    _, b = _split(b"batch B", 2, 3)
    with pytest.raises(service.MixedBatchError) as exc:
        service.recover_from_encoded([a["shares"][0], a["shares"][1], b["shares"][0]])
    assert exc.value.details["expected"]["threshold"] == 3
    assert exc.value.details["mismatches"][0]["threshold"] == 2


def test_mixed_batch_same_k_n_but_different_secret_length_rejected():
    _, a = _split(b"short", 3, 5)
    _, b = _split(b"x" * 4096, 3, 5)  # different block count
    with pytest.raises(service.MixedBatchError):
        service.recover_from_encoded([a["shares"][0], a["shares"][1], b["shares"][0]])


def test_corrupt_encoding_rejected_with_positions():
    _, result = _split()
    bad_batch = [result["shares"][0], "SSS1$not-valid-base64!!!", result["shares"][2]]
    with pytest.raises(service.InvalidShareEncodingError) as exc:
        service.recover_from_encoded(bad_batch)
    assert exc.value.details["errors"][0]["position"] == 1


def test_truncated_and_garbage_encodings_rejected():
    _, result = _split()
    token = result["shares"][1]
    for bad in [token[:20], "SSS1$", "@@@", "{not json", 12345, None]:
        with pytest.raises((service.InvalidShareEncodingError, service.ParameterError)):
            service.recover_from_encoded([result["shares"][0], bad, result["shares"][2]])


def test_json_object_shares_accepted_in_same_batch():
    _, result = _split()
    batch = [
        result["shares_json"][0],
        result["shares"][1],
        result["shares_json"][2],
    ]
    out = service.recover_from_encoded(batch)
    assert base64.b64decode(out["secret_b64"]) == b"acceptance-secret"


def test_duplicate_x_detected_even_with_different_encodings():
    _, result = _split(k=3, n=5)
    # same share index 1 submitted twice, one compact one JSON
    with pytest.raises(service.DuplicateIndexError) as exc:
        service.recover_from_encoded(
            [result["shares"][0], result["shares_json"][0], result["shares"][1]]
        )
    assert exc.value.details["duplicates"][0]["x"] == 1


def test_threshold_counted_by_distinct_x_not_token_count():
    _, result = _split(k=3, n=5)
    # 3 tokens but only 2 distinct x: must be refused as duplicate, not accepted.
    with pytest.raises(service.DuplicateIndexError):
        service.recover_from_encoded([result["shares"][0], result["shares"][0], result["shares"][1]])


# ---------------------------------------------------------------- tampering

def test_modified_share_fingerprint_mismatch_flags_failure():
    _, result = _split(b"the real secret value", 3, 5)
    malicious_token = flip_last_y(result["shares"][1])
    batch = [result["shares"][0], malicious_token, result["shares"][2]]
    with pytest.raises(service.RecoveredSecretInvalidError) as exc:
        service.recover_from_encoded(batch, expected_fingerprint=result["secret_fingerprint"])
    message = str(exc.value)
    assert "cannot identify which participant" in message
    assert exc.value.details["expected_fingerprint"] == result["secret_fingerprint"]
    # Crucial security note: the exception never carries reconstructed bytes.
    assert "secret_b64" not in str(exc.value.details)


def test_modified_share_without_fingerprint_either_invalid_or_wrong():
    _, result = _split(b"another secret", 3, 5)
    malicious = flip_last_y(result["shares"][2])
    try:
        out = service.recover_from_encoded([result["shares"][0], result["shares"][1], malicious])
    except service.RecoveredSecretInvalidError:
        pass  # length-header check caught it
    else:
        assert base64.b64decode(out["secret_b64"]) != b"another secret"
        assert out["secret_fingerprint"] != result["secret_fingerprint"]


# ---------------------------------------------------------------- parameters

def test_split_parameter_validation():
    with pytest.raises(service.ParameterError):
        service.split(b"", 3, 5)
    with pytest.raises(service.ParameterError):
        service.split(b"x", 1, 5)
    with pytest.raises(service.ParameterError):
        service.split(b"x", 6, 5)
    with pytest.raises(service.ParameterError):
        service.split("not-bytes", 3, 5)


def test_split_output_is_versioned():
    _, result = _split()
    assert result["version"] == 1
    assert result["field"] == "secp256k1-prime"
    assert all(t.startswith("SSS1$") for t in result["shares"])
    assert result["secret_fingerprint"].startswith("sha256:")


# ---------------------------------------------------------------- sealing demo

def test_seal_unseal_roundtrip():
    _, result = _split()
    token = seal_share(result["shares"][0], "correct horse battery")
    assert token.startswith("SEAL1$")
    assert unseal_share(token, "correct horse battery") == result["shares"][0]


def test_seal_wrong_passphrase_and_tampering_detected():
    _, result = _split()
    token = seal_share(result["shares"][0], "pw", n=2**12)
    with pytest.raises(SealingError):
        unseal_share(token, "wrong")
    tampered = token[:-4] + ("AAAA" if token[-4:] != "AAAA" else "BBBB")
    with pytest.raises(SealingError):
        unseal_share(tampered, "pw")
