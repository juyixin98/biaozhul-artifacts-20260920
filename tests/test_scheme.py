"""Tests for the Shamir split/recover core."""

import itertools
import os
import random

import pytest

from threshold_shares.scheme import split_secret, recover_secret, Share, CHUNK_SIZE


def test_every_3_of_5_subset_recovers_same_secret():
    secret = b"correct-horse-battery-staple"
    shares = split_secret(secret, 3, 5)
    for combo in itertools.combinations(shares, 3):
        recovered = recover_secret(list(combo), len(shares[0].ys))
        assert recovered == secret


@pytest.mark.parametrize("threshold,total", [(2, 2), (2, 3), (3, 5), (4, 4), (5, 9), (10, 10)])
def test_threshold_combinations(threshold, total):
    secret = os.urandom(77)
    shares = split_secret(secret, threshold, total)
    recovered = recover_secret(shares[:threshold], len(shares[0].ys))
    assert recovered == secret


@pytest.mark.parametrize("length", [0, 1, 31, 32, 33, 62, 63, 100, 1000])
def test_secret_lengths_including_block_boundaries(length):
    secret = bytes(range(256)) * (length // 256) + bytes(range(length % 256))
    shares = split_secret(secret, 3, 5)
    assert recover_secret(shares[1:4], len(shares[0].ys)) == secret


def test_randomized_property_any_threshold_subset():
    rng = random.Random(42)
    for trial in range(15):
        total = rng.randint(2, 12)
        threshold = rng.randint(2, total)
        secret = os.urandom(rng.randint(1, 500))
        shares = split_secret(secret, threshold, total)
        indices = rng.sample(range(total), threshold)
        recovered = recover_secret([shares[i] for i in indices], len(shares[0].ys))
        assert recovered == secret, f"trial {trial} ({threshold}-of-{total}) failed"


def test_invalid_parameters_rejected():
    with pytest.raises(ValueError):
        split_secret(b"x", 1, 3)
    with pytest.raises(ValueError):
        split_secret(b"x", 4, 3)
    with pytest.raises(ValueError):
        split_secret(b"x", 2, 256)
    with pytest.raises(ValueError):
        Share(x=0, ys=(1,))
    with pytest.raises(ValueError):
        Share(x=1, ys=())


def test_duplicate_x_coordinates_rejected():
    shares = split_secret(b"duplicate-x", 3, 5)
    blocks = len(shares[0].ys)
    with pytest.raises(ValueError, match="duplicate"):
        recover_secret([shares[0], shares[0], shares[2]], blocks)


def test_fewer_than_threshold_cannot_recover():
    """Core scheme: with only k-1 points the constant term is not determined."""
    secret = b"need more shares than this"
    shares = split_secret(secret, 4, 6)
    blocks = len(shares[0].ys)
    # Either structural validation fails, or garbage comes out -- never the
    # actual secret. This is why the service layer enforces the count.
    outcomes = []
    for combo in itertools.combinations(shares, 3):
        try:
            outcomes.append(recover_secret(list(combo), blocks))
        except ValueError:
            outcomes.append(None)
    assert all(outcome != secret for outcome in outcomes)


def test_modified_share_breaks_recovery_but_no_culprit_identified():
    """An unauthenticated modified share silently corrupts the output.

    The core layer returns either structurally-invalid bytes or wrong bytes;
    it has no information naming the malicious share. The fingerprint check at
    the service layer can flag *that* something is wrong (tested separately).
    """
    secret = b"unauthenticated shares"
    shares = split_secret(secret, 3, 5)
    bad_ys = list(shares[1].ys)
    bad_ys[0] = (bad_ys[0] ^ 1)
    bad = Share(x=shares[1].x, ys=tuple(bad_ys))
    with pytest.raises(ValueError):
        result = recover_secret([shares[0], bad, shares[2]], len(shares[0].ys))
        assert result != secret  # if it parses at all, it must not be the secret


def test_share_count_is_total_and_indices_are_one_based():
    shares = split_secret(b"idx", 3, 5)
    assert [s.x for s in shares] == [1, 2, 3, 4, 5]


def test_chunk_size_safely_below_prime():
    assert 8 * CHUNK_SIZE < 256  # 248-bit blocks vs 256-bit prime
