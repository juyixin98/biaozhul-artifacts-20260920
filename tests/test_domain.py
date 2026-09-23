"""Unit tests for pure domain rules and real Ed25519 cryptography."""
from __future__ import annotations

import pytest

from app import domain
from app.testsign import new_validator


def test_address_is_sha256_prefix_of_public_key():
    import hashlib

    v = new_validator()
    assert v["validator_address"] == hashlib.sha256(bytes.fromhex(v["public_key"])).hexdigest()[:40]


def test_real_signature_roundtrip_verifies():
    v = new_validator()
    fields = domain.VoteFields("chain-a", v["validator_address"], 7, 3, "prevote", "a" * 64)
    sig = bytes.fromhex(domain.sign_vote(v["private_key"], fields))
    pub = bytes.fromhex(v["public_key"])
    domain.verify_signature(fields, pub, sig)  # must not raise


def test_tampered_body_fails_signature():
    v = new_validator()
    fields = domain.VoteFields("chain-a", v["validator_address"], 7, 3, "prevote", "a" * 64)
    sig = bytes.fromhex(domain.sign_vote(v["private_key"], fields))
    tampered = domain.VoteFields("chain-a", v["validator_address"], 8, 3, "prevote", "a" * 64)
    with pytest.raises(domain.DomainError, match="signature verification failed"):
        domain.verify_signature(tampered, bytes.fromhex(v["public_key"]), sig)


def test_cross_chain_signature_is_invalid():
    """A signature made for chain A must not verify against a chain-B message."""
    v = new_validator()
    on_a = domain.VoteFields("chain-a", v["validator_address"], 7, 3, "prevote", "a" * 64)
    sig = bytes.fromhex(domain.sign_vote(v["private_key"], on_a))
    on_b = domain.VoteFields("chain-b", v["validator_address"], 7, 3, "prevote", "a" * 64)
    with pytest.raises(domain.DomainError):
        domain.verify_signature(on_b, bytes.fromhex(v["public_key"]), sig)


def test_wrong_key_fails_and_address_mismatch_detected():
    v1, v2 = new_validator(), new_validator()
    envelope = {
        "chain_id": "chain-a",
        "validator_address": v1["validator_address"],
        "height": 1, "round": 0, "vote_type": "prevote",
        "block_hash": "a" * 64, "public_key": v1["public_key"],
        "signature": domain.sign_vote(v1["private_key"], domain.VoteFields(
            "chain-a", v1["validator_address"], 1, 0, "prevote", "a" * 64)),
    }
    # v2 signing v1's body -> signature invalid
    fields, pub, _ = domain.validate_envelope(envelope)
    bad_sig = bytes.fromhex(domain.sign_vote(v2["private_key"], fields))
    with pytest.raises(domain.DomainError):
        domain.verify_signature(fields, pub, bad_sig)
    # A pubkey that doesn't hash to the claimed address is rejected structurally.
    with pytest.raises(domain.DomainError, match="does not match"):
        domain.validate_envelope({**envelope, "public_key": v2["public_key"]})


def test_evidence_id_order_independent():
    v = new_validator()

    def rec(block_hash):
        f = domain.VoteFields("c", v["validator_address"], 10, 4, "precommit", block_hash)
        return domain.canonical_vote_record(f, bytes.fromhex(v["public_key"]),
                                            bytes.fromhex(domain.sign_vote(v["private_key"], f)))

    r1, r2 = rec("a" * 64), rec("b" * 64)
    id_forward, body_forward = domain.evidence_identity([r1, r2])
    id_reverse, body_reverse = domain.evidence_identity([r2, r1])
    assert id_forward == id_reverse
    assert body_forward == body_reverse


def test_evidence_id_distinguishes_groups():
    v = new_validator()

    def rec(round_index, block_hash):
        f = domain.VoteFields("c", v["validator_address"], 10, round_index, "prevote", block_hash)
        return domain.canonical_vote_record(f, bytes.fromhex(v["public_key"]),
                                            bytes.fromhex(domain.sign_vote(v["private_key"], f)))

    id_r4 = domain.evidence_identity([rec(4, "a" * 64), rec(4, "b" * 64)])[0]
    id_r5 = domain.evidence_identity([rec(5, "a" * 64), rec(5, "b" * 64)])[0]
    assert id_r4 != id_r5


def test_epoch_boundary_mapping():
    assert domain.epoch_of_round(0, 10) == 0
    assert domain.epoch_of_round(9, 10) == 0
    assert domain.epoch_of_round(10, 10) == 1
    assert domain.epoch_of_round(19, 10) == 1
    assert domain.epoch_of_round(20, 10) == 2


def test_slash_math_is_integer_and_exact():
    assert domain.slash_power(1_000_000, 10_000) == 10_000      # 1%
    assert domain.slash_power(99, 10_000) == 0                 # floor
    assert domain.slash_power(12345, 10_000) == 123            # floor(123.45)
    assert domain.slash_power(0, 10_000) == 0


def test_malformed_envelopes_rejected():
    base = {
        "chain_id": "c", "validator_address": "a" * 40, "height": 1,
        "round": 0, "vote_type": "prevote", "block_hash": "b" * 64,
        "public_key": "p" * 64, "signature": "s" * 128,
    }
    for broken in [
        {**base, "height": -1},
        {**base, "round": "x"},
        {**base, "vote_type": "bogus"},
        {**base, "block_hash": "zz"},
        {**base, "signature": "zz"},
        {**base, "chain_id": ""},
        {"chain_id": "c"},
    ]:
        with pytest.raises(domain.DomainError):
            domain.validate_envelope(broken)
