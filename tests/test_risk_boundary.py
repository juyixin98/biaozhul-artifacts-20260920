"""Risk-boundary tests: one-chain halt and the DELTA timeout-gap failure.

These tests are the heart of the acceptance brief: they show what the local
HTLC pair protects against, where its boundary is, and that the system never
claims arbitrary cross-chain atomicity.
"""

from __future__ import annotations

import time

from web3 import Web3

from htlclib.harness import run_safe_abort, run_timeout_gap_loss

AMOUNT = Web3.to_wei(1, "ether")


def test_halt_before_reveal_within_delta_safely_aborts(env):
    """Chain A frozen 3s (< DELTA=20), no preimage revealed yet:
    blocks resume, deadlines pass, both lockers get their ETH back."""
    result = run_safe_abort(env, halt_seconds=3)
    assert result["halt_within_delta"] is True
    assert result["block_frozen_during_halt"] is True
    assert result["rpc_answered_during_halt"] is True  # node up, just no blocks
    assert result["block_advanced_after_resume"] is True
    assert result["states"] == {"A": "REFUNDED", "B": "REFUNDED"}
    assert result["contract_balances_wei"] == {"A": 0, "B": 0}


def test_timeout_gap_loss_when_halt_outlasts_delta(env):
    """Preimage revealed on B; chain A cannot include Bob's claim before T_A:
    split outcome B=CLAIMED / A=REFUNDED, Bob bears the loss, late claim
    rejected by the state mutex."""
    result = run_timeout_gap_loss(env)
    assert result["states"] == {"A": "REFUNDED", "B": "CLAIMED"}
    assert result["bob_lost_wei"] == str(AMOUNT)
    assert result["alice_gained_wei"] == str(AMOUNT)
    assert result["bob_late_claim_reverted"] is True
    assert result["bob_late_claim_error"] == "AlreadySettled"
    # funds never get stuck: one side claimed, the other refunded
    assert result["contract_balances_wei"] == {"A": 0, "B": 0}


def test_pending_claim_during_halt_cannot_be_included(env):
    """Direct check that a halted chain really refuses to advance time."""
    from htlclib.harness import lock_both, timelines
    _, t_a, t_b = timelines(env)
    locked = lock_both(env, AMOUNT, t_a, t_b)

    blk0 = env.w3a.eth.block_number
    env.chain_a.pause_mining()
    env.cli_a.claim(locked["id_a"], locked["preimage"],
                    env.bob.address, env.bob.pk, wait=False)
    time.sleep(2)
    # no new blocks while halted -> receipt cannot exist
    assert env.w3a.eth.block_number == blk0
    assert env.cli_a.view_swap(locked["id_a"]).state == "LOCKED"

    env.chain_a.resume_mining()
    env.chain_a.evm_mine()
    assert env.w3a.eth.block_number >= blk0 + 1
    assert env.cli_a.view_swap(locked["id_a"]).state == "CLAIMED"


def test_delta_convention_is_signed_and_enforced(env):
    """B's timelock is exactly DELTA seconds before A's; getters agree."""
    from htlclib.chains import DELTA_SECONDS
    from htlclib.harness import lock_both, timelines
    _, t_a, t_b = timelines(env)
    assert t_a - t_b == DELTA_SECONDS
    locked = lock_both(env, AMOUNT, t_a, t_b)
    va = env.cli_a.view_swap(locked["id_a"])
    vb = env.cli_b.view_swap(locked["id_b"])
    assert va.timelock - vb.timelock == DELTA_SECONDS
    # same hash lock on both legs
    assert va.hash_lock == vb.hash_lock == locked["hash_lock"].hex()
