"""End-to-end acceptance tests driving the deployed contracts over web3.py/Anvil.

These complement the Foundry unit tests: they exercise the real compiled
artifacts, real transactions and real block-timestamp manipulation on a running
local node, exactly as the Python backend would in production.

Coverage required by the brief:
  * start/cliff/end boundary behaviour
  * repeated claims
  * revocation interleaved with claims
  * non-divisible totals (integer dust)
  * conservation: final claimed + owner refund == initial locked amount
"""
from __future__ import annotations

import pytest

TOK = 10**18


def _create(env, total, start, cliff, end, beneficiary=None):
    env.approve_max()
    sid, _ = env.create(total, start, cliff, end, beneficiary)
    return sid


def test_chain_is_local_anvil(env):
    assert env.w3.is_connected()
    assert int(env.w3.eth.chain_id) == 31337


# --------------------------------------------------------------------------
# Start / cliff / end boundaries
# --------------------------------------------------------------------------
def test_before_cliff_zero_then_linear_to_end(env):
    start, cliff, end = 1_000, 1_200, 1_400
    total = 1000 * TOK
    sid = _create(env, total, start, cliff, end)

    env.travel(cliff - 1)
    assert int(env.vesting.functions.vestedAmount(sid, env.now()).call()) == 0
    assert int(env.vesting.functions.releasable(sid).call()) == 0

    # mid-vesting: total * (now-start)/(end-start)
    mid = (start + end) // 2
    env.travel(mid)
    expected = total * (mid - start) // (end - start)
    assert int(env.vesting.functions.vestedAmount(sid, env.now()).call()) == expected

    # exactly at end -> full amount
    env.travel(end)
    assert int(env.vesting.functions.vestedAmount(sid, env.now()).call()) == total

    # past end stays capped
    env.travel(end + 10_000)
    assert int(env.vesting.functions.vestedAmount(sid, env.now()).call()) == total


def test_cliff_equal_start_vests_immediately(env):
    start = 5_000
    total = 500 * TOK
    sid = _create(env, total, start, start, start + 500)
    env.travel(start + 250)
    assert int(env.vesting.functions.vestedAmount(sid, env.now()).call()) == total // 2


# --------------------------------------------------------------------------
# Repeated claims
# --------------------------------------------------------------------------
def test_repeated_claim_only_pays_new_delta(env):
    start = 10_000
    end = start + 1_000
    total = 1000 * TOK
    sid = _create(env, total, start, start, end)

    env.travel(start + 250)
    env.send(env.beneficiary, env.vesting.functions.release(sid))  # anyone can call
    first = total // 4
    assert env.bal(env.beneficiary.address) == first

    # immediate repeat: nothing vested since last claim -> reverts
    with pytest.raises(Exception):
        env.send(env.beneficiary, env.vesting.functions.release(sid))
    assert env.bal(env.beneficiary.address) == first

    env.travel(end)
    env.send(env.beneficiary, env.vesting.functions.release(sid))
    assert env.bal(env.beneficiary.address) == total
    assert env.bal(env.vesting.address) == 0


# --------------------------------------------------------------------------
# Revocation interleaved with claims; vested portion retained
# --------------------------------------------------------------------------
def test_claim_then_revoke_then_claim_keeps_vested(env):
    start = 20_000
    cliff = start + 100
    end = start + 400
    total = 1000 * TOK
    sid = _create(env, total, start, cliff, end)

    # 1) partial claim
    t1 = start + 160
    env.travel(t1)
    env.send(env.owner, env.vesting.functions.release(sid))
    claimed1 = total * (t1 - start) // (end - start)
    assert env.bal(env.beneficiary.address) == claimed1

    # 2) revoke later at t2; vested grows between t1 and t2
    t2 = start + 240
    env.travel(t2)
    vested_at_revoke = total * (t2 - start) // (end - start)
    owner_before = env.bal(env.owner.address)
    env.send(env.owner, env.vesting.functions.revoke(sid))
    refund = total - vested_at_revoke
    assert env.bal(env.owner.address) == owner_before + refund

    sched = env.vesting.functions.scheduleOf(sid).call()
    assert int(sched[7]) == t2  # revokedAt

    # 3) time passes to/past end, only the frozen vested remainder is claimable
    env.travel(end + 1000)
    env.send(env.owner, env.vesting.functions.release(sid))
    claimed2 = vested_at_revoke - claimed1
    assert env.bal(env.beneficiary.address) == claimed1 + claimed2 == vested_at_revoke

    # further claims revert
    with pytest.raises(Exception):
        env.send(env.owner, env.vesting.functions.release(sid))

    # CONSERVATION: beneficiary total + owner refund == initial lock
    assert env.bal(env.beneficiary.address) + refund == total
    assert env.bal(env.vesting.address) == 0


def test_revoke_before_cliff_refunds_all(env):
    start = 30_000
    cliff = start + 100
    end = start + 400
    total = 250 * TOK
    sid = _create(env, total, start, cliff, end)

    env.travel(cliff - 10)
    owner_before = env.bal(env.owner.address)
    env.send(env.owner, env.vesting.functions.revoke(sid))
    assert env.bal(env.owner.address) == owner_before + total
    assert env.bal(env.vesting.address) == 0

    env.travel(end)
    with pytest.raises(Exception):
        env.send(env.owner, env.vesting.functions.release(sid))


def test_revoke_then_beneficiary_still_collects_vested(env):
    start = 40_000
    end = start + 300
    total = 900 * TOK
    sid = _create(env, total, start, start, end)

    t = start + 210
    env.travel(t)
    vested = total * (t - start) // (end - start)
    env.send(env.owner, env.vesting.functions.revoke(sid))
    assert env.bal(env.vesting.address) == vested  # only vested remains

    # frozen even near the end
    env.travel(end - 1)
    assert int(env.vesting.functions.releasable(sid).call()) == vested

    env.send(env.beneficiary, env.vesting.functions.release(sid))
    assert env.bal(env.beneficiary.address) == vested
    assert env.bal(env.vesting.address) == 0


def test_double_revoke_reverts(env):
    start = 50_000
    end = start + 100
    sid = _create(env, 100 * TOK, start, start, end)
    env.travel(start + 50)
    env.send(env.owner, env.vesting.functions.revoke(sid))
    with pytest.raises(Exception):
        env.send(env.owner, env.vesting.functions.revoke(sid))


def test_non_owner_cannot_revoke(env):
    start = 60_000
    end = start + 100
    sid = _create(env, 100 * TOK, start, start, end)
    env.travel(start + 50)
    with pytest.raises(Exception):
        env.send(env.beneficiary, env.vesting.functions.revoke(sid))


# --------------------------------------------------------------------------
# Non-divisible totals: integer dust settles at end / on revoke
# --------------------------------------------------------------------------
@pytest.mark.parametrize("amount", [1001 * TOK, 999 * TOK, 3 * TOK, 777 * TOK + 123])
def test_non_divisible_full_vesting_conserves(env, amount):
    start = 70_000
    end = start + 300
    sid = _create(env, amount, start, start, end)

    env.travel(start + 200)
    env.send(env.owner, env.vesting.functions.release(sid))
    mid = amount * 200 // 300
    assert env.bal(env.beneficiary.address) == mid

    env.travel(end)
    env.send(env.owner, env.vesting.functions.release(sid))
    # dust delivered at end; beneficiary receives exactly the locked amount
    assert env.bal(env.beneficiary.address) == amount
    assert env.bal(env.vesting.address) == 0


def test_non_divisible_revoke_conservation(env):
    amount = 999 * TOK + 7  # not a multiple of the elapsed divisor
    start = 80_000
    end = start + 100
    sid = _create(env, amount, start, start, end)

    t = start + 33
    env.travel(t)
    vested = amount * (t - start) // (end - start)
    env.send(env.owner, env.vesting.functions.release(sid))

    owner_before = env.bal(env.owner.address)
    env.send(env.owner, env.vesting.functions.revoke(sid))
    refund = amount - vested
    assert env.bal(env.owner.address) == owner_before + refund

    # nothing more to claim after the frozen vested amount was already taken
    env.travel(end)
    with pytest.raises(Exception):
        env.send(env.owner, env.vesting.functions.release(sid))

    assert env.bal(env.beneficiary.address) + refund == amount
    assert env.bal(env.vesting.address) == 0


# --------------------------------------------------------------------------
# Global conservation across a multi-step, interleaved lifecycle
# --------------------------------------------------------------------------
def test_conservation_interleaved_lifecycle(env):
    start = 90_000
    cliff = start + 50
    end = start + 500
    amount = 12345 * TOK + 678  # odd total
    sid = _create(env, amount, start, cliff, end)

    owner = env.owner.address
    bene = env.beneficiary.address
    owner_start_bal = env.bal(owner)

    # claim near cliff
    env.travel(start + 60)
    env.send(env.owner, env.vesting.functions.release(sid))
    # claim again mid-way
    env.travel(start + 300)
    env.send(env.owner, env.vesting.functions.release(sid))
    # revoke
    revoke_t = start + 400
    env.travel(revoke_t)
    env.send(env.owner, env.vesting.functions.revoke(sid))
    # final claim of frozen vested remainder
    env.travel(end + 50)
    env.send(env.owner, env.vesting.functions.release(sid))

    vested_final = amount * (revoke_t - start) // (end - start)
    # beneficiary got exactly vested-at-revoke
    assert env.bal(bene) == vested_final
    # owner recovered the unvested remainder (balance net of the original lock)
    assert env.bal(owner) - owner_start_bal == amount - vested_final
    # the escrow holds nothing
    assert env.bal(env.vesting.address) == 0
    # identity required by the brief
    assert env.bal(bene) + (env.bal(owner) - owner_start_bal) == amount
