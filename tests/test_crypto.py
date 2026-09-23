"""Tests for SHA-256 snapshot/quote binding and tamper evidence."""

from __future__ import annotations

import hashlib
import json

from app.crypto import canonical_json, digest_pool, digest_quote, verify_quote_digest
from app.engine import quote_swap
from app.models import Pool, Position


def _pool() -> Pool:
    return Pool(
        pool_id="digest-pool", token0="USDC", token1="WETH",
        fee_numerator=3000, fee_denominator=1_000_000, current_tick=100,
        positions=(Position(0, 200, 10**30),),
    )


def test_digest_is_real_sha256() -> None:
    pool = _pool()
    expected = hashlib.sha256(canonical_json(pool.canonical())).hexdigest()
    assert digest_pool(pool) == expected
    assert len(expected) == 64


def test_digest_changes_with_any_snapshot_field() -> None:
    base = _pool()
    variants = [
        Pool(pool_id="digest-pool", token0="USDC", token1="WETH", fee_numerator=3000,
             fee_denominator=1_000_000, current_tick=101,
             positions=(Position(0, 200, 10**30),)),
        Pool(pool_id="digest-pool", token0="USDC", token1="WETH", fee_numerator=3001,
             fee_denominator=1_000_000, current_tick=100,
             positions=(Position(0, 200, 10**30),)),
        Pool(pool_id="digest-pool", token0="DAI", token1="WETH", fee_numerator=3000,
             fee_denominator=1_000_000, current_tick=100,
             positions=(Position(0, 200, 10**30),)),
        Pool(pool_id="digest-pool", token0="USDC", token1="WETH", fee_numerator=3000,
             fee_denominator=1_000_000, current_tick=100,
             positions=(Position(0, 200, 10**30 + 1),)),
        Pool(pool_id="digest-pool", token0="USDC", token1="WETH", fee_numerator=3000,
             fee_denominator=1_000_000, current_tick=100,
             positions=(Position(0, 201, 10**30),)),
    ]
    for other in variants:
        assert digest_pool(other) != digest_pool(base), other.pool_id


def test_quote_digest_verifies_and_is_tamper_evident() -> None:
    pool = _pool()
    result = quote_swap(pool, zero_for_one=True, amount_in=10**18)
    assert verify_quote_digest(result)

    # Tampering with a single output breaks the digest.
    tampered = __import__("dataclasses").replace(result, amount_out=str(int(result.amount_out) + 1))
    assert not verify_quote_digest(tampered)

    # Tampering with a segment breaks it too.
    segs = list(result.segments)
    segs[0] = __import__("dataclasses").replace(segs[0], fee_input=str(int(segs[0].fee_input) + 1))
    tampered2 = __import__("dataclasses").replace(result, segments=tuple(segs))
    assert not verify_quote_digest(tampered2)


def test_quote_digest_binds_to_pool_digest() -> None:
    pool = _pool()
    result = quote_swap(pool, zero_for_one=True, amount_in=10**18)
    payload = json.loads(json.dumps(result.to_dict(), sort_keys=True))
    assert payload["pool_digest"] == digest_pool(pool)
    assert payload["quote_digest"] == digest_quote(result.pool_digest, result)
