"""End-to-end acceptance tests over the FastAPI HTTP API against real Anvil.

Mirrors the Foundry contract tests through the web3.py + HTTP stack:
  * start/cliff/end boundary queries
  * duplicate claims pay nothing twice
  * revoke <-> claim interleaving (both orders)
  * non-divisible total: final released + refunded == locked, exactly
"""
from __future__ import annotations

import time

import pytest

TOTAL = 1_000 * 10**18
CLIFF = 100
DURATION = 1_000


def _now(client) -> int:
    return client.w3.eth.get_block("latest")["timestamp"]


def _create(client, chain, amount=TOTAL, revocable=True, start=None,
            cliff=CLIFF, duration=DURATION) -> int:
    result = client.create_schedule(
        owner_key=chain["owner_key"],
        beneficiary=chain["beneficiary"],
        amount=amount,
        # small forward offset so the creation tx itself never lands at start
        start_timestamp=start if start is not None else _now(client) + 10,
        cliff_duration=cliff,
        vesting_duration=duration,
        revocable=revocable,
    )
    return result["schedule_id"]


def _warp(client, ts: int) -> None:
    """Move the chain clock to ts and mine (view-only checkpoints)."""
    client.pin_next_timestamp(ts)


def _at(client, ts: int, action):
    """Execute an action transaction in a block whose timestamp is exactly ts."""
    client.pin_next_timestamp(ts)
    return action()


# ---------------------------------------------------------------------
# Boundary queries
# ---------------------------------------------------------------------


def test_http_boundaries_before_cliff_and_at_end(http_client, client, chain):
    start = _now(client) + 10
    sid = _create(client, chain, start=start)

    r = http_client.get(f"/schedules/{sid}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_amount"] == TOTAL
    assert body["vested_now"] == 0
    assert body["releasable_now"] == 0
    assert body["start"] == start
    assert body["cliff"] == start + CLIFF
    assert body["end"] == start + DURATION

    # claiming before cliff reverts (pin block time to inside the cliff)
    _warp(client, start + CLIFF - 1)
    r = http_client.post(f"/schedules/{sid}/release")
    assert r.status_code == 400

    # at end everything vests
    _warp(client, start + DURATION)
    r = http_client.get(f"/schedules/{sid}")
    assert r.json()["vested_now"] == TOTAL


def test_http_health_and_token_balance(http_client):
    r = http_client.get("/health")
    assert r.status_code == 200
    chain = r.json()["chain"]
    assert chain["chain_id"] == 31337
    assert chain["token_symbol"] == "SYN"

    r = http_client.get(f"/tokens/0x0000000000000000000000000000000000000001")
    assert r.status_code == 200
    assert r.json()["balance"] == 0


# ---------------------------------------------------------------------
# Duplicate claims
# ---------------------------------------------------------------------


def test_duplicate_claims_pay_only_delta(client, chain):
    start = _now(client)
    sid = _create(client, chain, start=start, revocable=False)

    half = start + DURATION // 2
    _at(client, half, lambda: client.release(chain["beneficiary_key"], sid))

    # a second claim executed in a block with the IDENTICAL timestamp pays nothing
    with pytest.raises(Exception):
        _at(client, half, lambda: client.release(chain["beneficiary_key"], sid))

    _at(client, start + DURATION, lambda: client.release(chain["beneficiary_key"], sid))

    view = client.get_schedule(sid)
    assert view.released_amount == TOTAL
    assert view.refunded_amount == 0
    assert view.total_amount - view.released_amount - view.refunded_amount == 0


# ---------------------------------------------------------------------
# Revoke / claim interleaving
# ---------------------------------------------------------------------


def test_revoke_then_claim_conservation(client, chain):
    start = _now(client)
    sid = _create(client, chain, start=start, revocable=True)
    owner_before = client.token_balance(chain["owner"])

    # revoke halfway: half refunded, half kept for beneficiary
    _at(client, start + DURATION // 2,
        lambda: client.revoke(chain["owner_key"], sid))

    refund = client.token_balance(chain["owner"]) - owner_before
    assert refund == TOTAL - TOTAL // 2

    # far in the future, the post-revoke claim still pays only the vested half
    _at(client, start + DURATION * 10,
        lambda: client.release(chain["beneficiary_key"], sid))

    view = client.get_schedule(sid)
    assert view.revoked is True
    assert view.released_amount == TOTAL // 2
    assert view.refunded_amount == refund
    assert view.released_amount + view.refunded_amount == TOTAL
    # escrow empty for this (only) schedule
    assert client.token_balance(chain["vesting"]) == 0


def test_claim_revoke_claim_interleave(client, chain):
    start = _now(client)
    sid = _create(client, chain, start=start, revocable=True)
    owner_before = client.token_balance(chain["owner"])

    # claim 25%
    _at(client, start + DURATION // 4,
        lambda: client.release(chain["beneficiary_key"], sid))

    # revoke at 60%
    _at(client, start + DURATION * 6 // 10,
        lambda: client.revoke(chain["owner_key"], sid))
    refund = client.token_balance(chain["owner"]) - owner_before
    assert refund == TOTAL - TOTAL * 6 // 10

    # claim the 35% remainder (vesting frozen at 60%)
    _at(client, start + DURATION * 10,
        lambda: client.release(chain["beneficiary_key"], sid))

    # no further vesting after revoke: a later claim at an even later time reverts
    with pytest.raises(Exception):
        _at(client, start + DURATION * 20,
            lambda: client.release(chain["beneficiary_key"], sid))

    view = client.get_schedule(sid)
    assert view.released_amount + view.refunded_amount == TOTAL
    assert client.token_balance(chain["vesting"]) == 0


def test_revoke_during_cliff_refunds_all(client, chain):
    start = _now(client)
    sid = _create(client, chain, start=start, revocable=True)
    owner_before = client.token_balance(chain["owner"])

    _at(client, start + CLIFF - 1,
        lambda: client.revoke(chain["owner_key"], sid))
    assert client.token_balance(chain["owner"]) - owner_before == TOTAL

    with pytest.raises(Exception):
        _at(client, start + CLIFF,
            lambda: client.release(chain["beneficiary_key"], sid))

    view = client.get_schedule(sid)
    assert view.released_amount == 0
    assert view.refunded_amount == TOTAL


def test_cannot_revoke_irrevocable(client, chain):
    sid = _create(client, chain, revocable=False)
    with pytest.raises(Exception):
        _at(client, _now(client) + 1,
            lambda: client.revoke(chain["owner_key"], sid))


# ---------------------------------------------------------------------
# Non-divisible total
# ---------------------------------------------------------------------


def test_non_divisible_total_no_dust_lost(client, chain):
    amount = 7  # 7 base units over 1000s -> rounding every checkpoint
    start = _now(client)
    sid = _create(client, chain, amount=amount, start=start, duration=DURATION)

    _at(client, start + DURATION // 3,
        lambda: client.release(chain["beneficiary_key"], sid))

    _at(client, start + DURATION * 2 // 3,
        lambda: client.revoke(chain["owner_key"], sid))
    _at(client, start + DURATION,
        lambda: client.release(chain["beneficiary_key"], sid))

    view = client.get_schedule(sid)
    assert view.released_amount + view.refunded_amount == amount
    assert client.token_balance(chain["vesting"]) == 0


def test_non_divisible_total_full_vest(client, chain):
    amount = 3
    start = _now(client)
    sid = _create(client, chain, amount=amount, start=start, revocable=False,
                  duration=DURATION)

    _at(client, start + DURATION // 2,
        lambda: client.release(chain["beneficiary_key"], sid))
    _at(client, start + DURATION,
        lambda: client.release(chain["beneficiary_key"], sid))

    assert client.token_balance(chain["beneficiary"]) == amount
    view = client.get_schedule(sid)
    assert view.released_amount == amount
