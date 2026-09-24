"""Unit tests for the real cryptographic segment workload."""

import hashlib

import pytest

from action_cancel_server import crypto


def test_segment_digest_is_pbkdf2_and_deterministic():
    salt = b"seg-v1|g1|0"
    expected = hashlib.pbkdf2_hmac(
        "sha256", b"alpha", salt, 1000, dklen=32
    ).hex()
    assert crypto.segment_digest("g1", 0, "alpha", 1000) == expected
    assert (
        crypto.segment_digest("g1", 0, "alpha", 1000)
        == crypto.segment_digest("g1", 0, "alpha", 1000)
    )


def test_salt_binds_goal_id_index_and_iterations():
    base = crypto.segment_digest("g1", 0, "alpha", 1000)
    assert crypto.segment_digest("g2", 0, "alpha", 1000) != base
    assert crypto.segment_digest("g1", 1, "alpha", 1000) != base
    assert crypto.segment_digest("g1", 0, "alpha", 1001) != base


def test_swapping_inputs_changes_every_downstream_hash():
    a = [crypto.segment_digest("g", i, t, 100) for i, t in enumerate(["x", "y"])]
    b = [crypto.segment_digest("g", i, t, 100) for i, t in enumerate(["y", "x"])]
    assert a != b


def test_chain_order_sensitive_and_genesis_documented():
    h0 = crypto.chain_step(crypto.CHAIN_GENESIS, "aa")
    h1 = crypto.chain_step(h0, "bb")
    assert h0 != h1
    assert crypto.chain_step(crypto.CHAIN_GENESIS, "aa") == h0  # deterministic
    # swapped order yields a different final digest
    swapped = crypto.chain_step(
        crypto.chain_step(crypto.CHAIN_GENESIS, "bb"), "aa"
    )
    assert swapped != h1


def test_goal_fingerprint_detects_parameter_changes():
    f0 = crypto.goal_fingerprint("g", ["a", "b"], 100, 0)
    assert f0 == crypto.goal_fingerprint("g", ["a", "b"], 100, 0)
    assert crypto.goal_fingerprint("g", ["a", "b"], 101, 0) != f0
    assert crypto.goal_fingerprint("g", ["a", "c"], 100, 0) != f0
    assert crypto.goal_fingerprint("g", ["a"], 100, 0) != f0
    assert crypto.goal_fingerprint("g", ["a", "b"], 100, 1) != f0
    # order is significant
    assert crypto.goal_fingerprint("g", ["b", "a"], 100, 0) != f0


def test_input_fingerprint_and_constant_time_compare():
    fp = crypto.input_fingerprint("secret")
    assert fp == hashlib.sha256(b"secret").hexdigest()
    assert crypto.constant_time_equal(fp, fp)
    assert not crypto.constant_time_equal(fp, hashlib.sha256(b"secrex").hexdigest())


def test_bad_iterations_rejected():
    with pytest.raises(ValueError):
        crypto.segment_digest("g", 0, "x", 0)
