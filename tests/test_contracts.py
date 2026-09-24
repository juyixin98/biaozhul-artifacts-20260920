"""Contract-behavior tests over real local Anvil chains (Python / web3.py).

Covers acceptance items: deadline boundary, wrong preimage, double claim and
double refund, plus the claim/refund mutex in both directions.
"""

from __future__ import annotations

import pytest
from web3 import Web3

from htlclib.chains import DELTA_SECONDS
from htlclib.contract import new_secret
from htlclib.harness import lock_both, timelines

AMOUNT = Web3.to_wei(1, "ether")


def test_happy_path_both_legs_claimed(env):
    from htlclib.harness import run_happy_swap
    result = run_happy_swap(env)
    assert result["states"] == {"A": "CLAIMED", "B": "CLAIMED"}
    assert result["contract_balances_wei"] == {"A": 0, "B": 0}
    assert result["timeline"]["delta"] == DELTA_SECONDS


def test_swap_id_matches_on_chain_derivation(env):
    from htlclib.contract import derive_swap_id
    _, t_a, t_b = timelines(env)
    locked = lock_both(env, AMOUNT, t_a, t_b)
    expect_a = derive_swap_id(locked["hash_lock"], env.alice.address,
                              env.bob.address, AMOUNT, t_a)
    expect_b = derive_swap_id(locked["hash_lock"], env.bob.address,
                              env.alice.address, AMOUNT, t_b)
    assert expect_a == locked["id_a"]
    assert expect_b == locked["id_b"]


def test_wrong_preimage_reverts_and_keeps_locked(env):
    _, t_a, t_b = timelines(env)
    locked = lock_both(env, AMOUNT, t_a, t_b)
    bogus = b"\x11" * 32
    with pytest.raises(Exception) as ei:
        env.cli_a.claim(locked["id_a"], bogus, env.bob.address, env.bob.pk)
    assert "3851b52e" in str(ei.value)  # WrongPreimage selector
    assert env.cli_a.view_swap(locked["id_a"]).state == "LOCKED"
    # ... and the correct preimage still works afterwards.
    env.cli_a.claim(locked["id_a"], locked["preimage"],
                    env.bob.address, env.bob.pk)
    assert env.cli_a.view_swap(locked["id_a"]).state == "CLAIMED"


def test_refund_reverts_before_deline_and_works_at_deadline(env):
    _, t_a, t_b = timelines(env)
    locked = lock_both(env, AMOUNT, t_a, t_b)

    # one second before T_B
    env.chain_b.evm_warp(t_b - 1)
    with pytest.raises(Exception) as ei:
        env.cli_b.refund(locked["id_b"], env.bob.address, env.bob.pk)
    assert "cb6c86dd" in str(ei.value)  # TooEarly

    # exactly AT T_B: block.timestamp == timelock is refundable
    env.chain_b.evm_warp(t_b)
    rcpt = env.cli_b.refund(locked["id_b"], env.bob.address, env.bob.pk)
    assert rcpt["status"] == 1
    assert env.cli_b.view_swap(locked["id_b"]).state == "REFUNDED"


def test_refund_only_by_locker(env):
    _, t_a, t_b = timelines(env)
    locked = lock_both(env, AMOUNT, t_a, t_b)
    env.chain_b.evm_warp(t_b)
    with pytest.raises(Exception) as ei:
        # Alice is the receiver of B's leg, not the locker
        env.cli_b.refund(locked["id_b"], env.alice.address, env.alice.pk)
    assert "fc21064f" in str(ei.value)  # NotSender


def test_double_claim_rejected(env):
    _, t_a, t_b = timelines(env)
    locked = lock_both(env, AMOUNT, t_a, t_b)
    env.cli_a.claim(locked["id_a"], locked["preimage"],
                    env.bob.address, env.bob.pk)
    with pytest.raises(Exception) as ei:
        env.cli_a.claim(locked["id_a"], locked["preimage"],
                        env.bob.address, env.bob.pk)
    assert "4195444e" in str(ei.value)  # AlreadySettled(CLAIMED)


def test_double_refund_rejected(env):
    _, t_a, t_b = timelines(env)
    locked = lock_both(env, AMOUNT, t_a, t_b)
    env.chain_a.evm_warp(t_a)
    env.cli_a.refund(locked["id_a"], env.alice.address, env.alice.pk)
    with pytest.raises(Exception) as ei:
        env.cli_a.refund(locked["id_a"], env.alice.address, env.alice.pk)
    assert "4195444e" in str(ei.value)  # AlreadySettled(REFUNDED)


def test_claim_after_refund_and_refund_after_claim_rejected(env):
    _, t_a, t_b = timelines(env)
    locked = lock_both(env, AMOUNT, t_a, t_b)

    # B: refund first -> claim impossible
    env.chain_b.evm_warp(t_b)
    env.cli_b.refund(locked["id_b"], env.bob.address, env.bob.pk)
    with pytest.raises(Exception) as ei:
        env.cli_b.claim(locked["id_b"], locked["preimage"],
                        env.alice.address, env.alice.pk)
    assert "4195444e" in str(ei.value)

    # A: claim first -> refund impossible even past deadline
    env.cli_a.claim(locked["id_a"], locked["preimage"],
                    env.bob.address, env.bob.pk)
    env.chain_a.evm_warp(t_a + 10)
    with pytest.raises(Exception) as ei:
        env.cli_a.refund(locked["id_a"], env.alice.address, env.alice.pk)
    assert "4195444e" in str(ei.value)


def test_claim_while_locked_after_deadline_still_wins(env):
    """Documented boundary: a revealed preimage beats the clock."""
    _, t_a, _ = timelines(env)
    locked = lock_both(env, AMOUNT, t_a, t_a - DELTA_SECONDS)
    env.chain_a.evm_warp(t_a + 5)
    rcpt = env.cli_a.claim(locked["id_a"], locked["preimage"],
                           env.bob.address, env.bob.pk)
    assert rcpt["status"] == 1
    assert env.cli_a.view_swap(locked["id_a"]).state == "CLAIMED"


def test_timelock_in_the_past_rejected(env):
    preimage, hl = new_secret()
    now = env.w3a.eth.get_block("latest")["timestamp"]
    with pytest.raises(Exception) as ei:
        env.cli_a.lock(hl, env.alice.address, env.alice.pk,
                       env.bob.address, AMOUNT, now)  # timelock == now -> reject
    assert "321208cd" in str(ei.value)  # TimelockInPast


def test_unknown_swap_reads_as_nonexistent(env):
    view = env.cli_a.view_swap(b"\x00" * 32)
    assert view.state == "NONEXISTENT"
    assert view.state_code == 0
