"""Reusable two-chain HTLC scenario harness shared by demo and tests.

Flows use the Anvil default accounts #0 (Alice) and #1 (Bob):
  leg on chain A: Alice locks -> Bob
  leg on chain B: Bob   locks -> Alice
Timelines obey the convention T_B = T_A - DELTA.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from web3 import Web3

from .chains import (
    ALICE_PK,
    BOB_PK,
    DELTA_SECONDS,
    TTL_A_SECONDS,
    AnvilChain,
    spawn_both,
)
from .contract import HtlcClient, deploy, new_secret


@dataclass
class Party:
    address: str
    pk: str


@dataclass
class Env:
    chain_a: AnvilChain
    chain_b: AnvilChain
    w3a: Web3
    w3b: Web3
    cli_a: HtlcClient
    cli_b: HtlcClient
    alice: Party
    bob: Party
    deployment_file: Path

    def stop(self) -> None:
        self.chain_a.stop()
        self.chain_b.stop()


def build_env(log_dir: Path | str = "logs",
              deployment_file: Path | str = "deployment.json",
              ttl_a: int = TTL_A_SECONDS,
              delta: int = DELTA_SECONDS) -> Env:
    chain_a, chain_b = spawn_both(log_dir)
    try:
        w3a = chain_a.w3
        w3b = chain_b.w3
        alice = Party(w3a.eth.account.from_key(ALICE_PK).address, ALICE_PK)
        bob = Party(w3a.eth.account.from_key(BOB_PK).address, BOB_PK)

        ctr_a, _ = deploy(w3a, alice.address, alice.pk)
        ctr_b, _ = deploy(w3b, alice.address, alice.pk)
        dep_path = Path(deployment_file)
        dep_path.write_text(
            f'{{"chain_a": {{"rpc": "{chain_a.rpc}", "chain_id": {chain_a.chain_id}, '
            f'"address": "{ctr_a.address}"}}, '
            f'"chain_b": {{"rpc": "{chain_b.rpc}", "chain_id": {chain_b.chain_id}, '
            f'"address": "{ctr_b.address}"}}}}\n')
        return Env(chain_a, chain_b, w3a, w3b,
                   HtlcClient(w3a, ctr_a.address),
                   HtlcClient(w3b, ctr_b.address),
                   alice, bob, dep_path)
    except Exception:
        chain_a.stop()
        chain_b.stop()
        raise


def timelines(env: Env, ttl_a: int = TTL_A_SECONDS,
              delta: int = DELTA_SECONDS) -> tuple[int, int, int]:
    now = env.w3a.eth.get_block("latest")["timestamp"]
    t_a = now + ttl_a
    t_b = t_a - delta
    return now, t_a, t_b


def lock_both(env: Env, amount_wei: int, t_a: int, t_b: int,
              preimage: bytes | None = None,
              hash_lock: bytes | None = None) -> dict[str, Any]:
    if preimage is None:
        preimage, hash_lock = new_secret()
    assert hash_lock is not None
    id_a, rcpt_a = env.cli_a.lock(hash_lock, env.alice.address, env.alice.pk,
                                  env.bob.address, amount_wei, t_a)
    id_b, rcpt_b = env.cli_b.lock(hash_lock, env.bob.address, env.bob.pk,
                                  env.alice.address, amount_wei, t_b)
    return {"preimage": preimage, "hash_lock": hash_lock,
            "id_a": id_a, "id_b": id_b,
            "t_a": t_a, "t_b": t_b,
            "blocks": (rcpt_a["blockNumber"], rcpt_b["blockNumber"])}


def claimed_preimage(client: HtlcClient, receipt) -> bytes:
    logs = client.contract.events.Claimed().process_receipt(receipt)
    return bytes(logs[0]["args"]["preimage"])


def states(env: Env, id_a: bytes, id_b: bytes) -> tuple[str, str]:
    return env.cli_a.view_swap(id_a).state, env.cli_b.view_swap(id_b).state


def run_happy_swap(env: Env, amount_wei: int | None = None,
                   ttl_a: int = TTL_A_SECONDS,
                   delta: int = DELTA_SECONDS) -> dict[str, Any]:
    amount_wei = amount_wei or Web3.to_wei(1, "ether")
    _, t_a, t_b = timelines(env, ttl_a, delta)
    locked = lock_both(env, amount_wei, t_a, t_b)

    # Alice reveals the preimage claiming her leg on B.
    rcpt_b = env.cli_b.claim(locked["id_b"], locked["preimage"],
                             env.alice.address, env.alice.pk)
    # Bob extracts the now-public preimage from B's Claimed event and claims A.
    preimage = claimed_preimage(env.cli_b, rcpt_b)
    rcpt_a = env.cli_a.claim(locked["id_a"], preimage,
                             env.bob.address, env.bob.pk)

    sa, sb = states(env, locked["id_a"], locked["id_b"])
    return {
        "timeline": {"T_A": t_a, "T_B": t_b, "delta": t_a - t_b},
        "ids": {"A": locked["id_a"].hex(), "B": locked["id_b"].hex()},
        "states": {"A": sa, "B": sb},
        "contract_balances_wei": {"A": env.cli_a.balance(), "B": env.cli_b.balance()},
        "claim_blocks": {"A": rcpt_a["blockNumber"], "B": rcpt_b["blockNumber"]},
        "amount_wei": str(amount_wei),
    }


def run_safe_abort(env: Env, halt_seconds: int = 3,
                   ttl_a: int = 60, delta: int = 20) -> dict[str, Any]:
    """Chain A halts BEFORE any preimage reveal, halt shorter than DELTA:
    no money is lost; after the deadlines both legs simply refund."""
    amount_wei = Web3.to_wei(1, "ether")
    now, t_a, t_b = timelines(env, ttl_a, delta)
    locked = lock_both(env, amount_wei, t_a, t_b)

    block_before = env.w3a.eth.block_number
    env.chain_a.pause_mining()
    time.sleep(halt_seconds)
    frozen_block = env.w3a.eth.block_number
    node_up = env.w3a.is_connected()  # RPC answers, but no new blocks
    env.chain_a.resume_mining()
    env.chain_a.evm_mine()
    block_after = env.w3a.eth.block_number

    # No reveal ever happened. Deadlines pass; each locker refunds their own leg.
    env.chain_b.evm_warp(t_b)
    rcpt_b = env.cli_b.refund(locked["id_b"], env.bob.address, env.bob.pk)
    env.chain_a.evm_warp(t_a)
    rcpt_a = env.cli_a.refund(locked["id_a"], env.alice.address, env.alice.pk)

    sa, sb = states(env, locked["id_a"], locked["id_b"])
    return {
        "halt_within_delta": halt_seconds < delta,
        "halt_seconds": halt_seconds,
        "delta": delta,
        "rpc_answered_during_halt": node_up,
        "block_frozen_during_halt": frozen_block == block_before,
        "block_advanced_after_resume": block_after > block_before,
        "states": {"A": sa, "B": sb},
        "contract_balances_wei": {"A": env.cli_a.balance(), "B": env.cli_b.balance()},
        "refund_blocks": {"A": rcpt_a["blockNumber"], "B": rcpt_b["blockNumber"]},
    }


def run_timeout_gap_loss(env: Env, ttl_a: int = 40, delta: int = 20) -> dict[str, Any]:
    """Boundary demo: preimage is revealed, then chain A cannot produce Bob's
    claim before T_A (halt longer than DELTA). Alice's A leg refunds first,
    while Alice already holds B's claim. Bob bears the loss.

    This is the documented HTLC timeout-gap failure; the coordinator can detect
    and label the state but cannot prevent it.
    """
    amount_wei = Web3.to_wei(1, "ether")
    now, t_a, t_b = timelines(env, ttl_a, delta)
    locked = lock_both(env, amount_wei, t_a, t_b)

    # 1) Alice claims on B one second before B's deadline: preimage goes public.
    env.chain_b.evm_warp(t_b - 1)
    rcpt_b = env.cli_b.claim(locked["id_b"], locked["preimage"],
                             env.alice.address, env.alice.pk)

    # 2) Chain A halts (no blocks) at the same moment: Bob cannot get his claim
    #    included while A is frozen, and the freeze outlasts DELTA.
    env.chain_a.pause_mining()
    time.sleep(1)
    block_frozen = env.w3a.eth.get_block("latest")["timestamp"] < t_a

    # 3) When A resumes it is already past T_A. Inclusion order at expiry decides
    #    everything; here Alice's refund is mined first, after which the state
    #    mutex rejects Bob's claim. (On a real chain ordering is a fee/relay race;
    #    the point is that no rule lets both sides win once DELTA is exceeded.)
    env.chain_a.resume_mining()
    env.chain_a.evm_warp(t_a + 1)
    rcpt_a = env.cli_a.refund(locked["id_a"], env.alice.address, env.alice.pk)
    bob_claim_reverted = False
    bob_claim_error = ""
    try:
        env.cli_a.claim(locked["id_a"], locked["preimage"],
                        env.bob.address, env.bob.pk)
    except Exception as exc:
        bob_claim_reverted = True
        bob_claim_error = _revert_reason(exc)

    sa, sb = states(env, locked["id_a"], locked["id_b"])
    return {
        "timeline": {"now": now, "T_A": t_a, "T_B": t_b, "delta": delta},
        "states": {"A": sa, "B": sb},
        "B_claimed_by": "Alice",
        "A_refunded_to": "Alice",
        "a_still_locked_before_resume": block_frozen,
        "bob_lost_wei": str(amount_wei),
        "alice_gained_wei": str(amount_wei),
        "contract_balances_wei": {"A": env.cli_a.balance(), "B": env.cli_b.balance()},
        "blocks": {"B_claim": rcpt_b["blockNumber"], "A_refund": rcpt_a["blockNumber"]},
        "bob_late_claim_reverted": bob_claim_reverted,
        "bob_late_claim_error": bob_claim_error,
        "lesson": (
            "reveal + halt longer than DELTA => one leg CLAIMED, other REFUNDED; "
            "the party that revealed late assigns the loss to its counterparty"
        ),
    }


def _revert_reason(exc: Exception) -> str:
    msg = str(exc)
    for marker in ("AlreadySettled", "TooEarly", "WrongPreimage",
                   "NotSender", "UnknownSwap", "SwapExists",
                   "execution reverted"):
        if marker in msg:
            return marker
    # Custom errors arrive as the selector + args tuple from the node.
    # 0x4195444e = AlreadySettled(State) from src/HashedTimelock.sol.
    selectors = {
        "0x4195444e": "AlreadySettled",
        "0xcb6c86dd": "TooEarly",
        "0x3851b52e": "WrongPreimage",
        "0xfc21064f": "NotSender",
        "0x6ea07af0": "UnknownSwap",
        "0xafa955b9": "SwapExists",
        "0x321208cd": "TimelockInPast",
    }
    for sel, name in selectors.items():
        if sel in msg:
            return name
    return msg[:160]
