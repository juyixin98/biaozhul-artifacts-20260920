"""End-to-end walkthrough against a local Anvil node.

Prereqs (see README.md):
  1. anvil running on http://127.0.0.1:8545
  2. contracts deployed via scripts/deploy.sh (writes deploy/addresses.json)

Run:
  .venv/bin/python examples/demo.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api import config
from api.client import VestingClient

ONE = 10**18


def main() -> None:
    c = VestingClient()
    status = c.chain_status()
    print(f"chain #{status['chain_id']} block={status['block_number']} "
          f"token={status['token_symbol']} escrow={status['escrow_balance']}")

    owner_key = config.OWNER_PRIVATE_KEY
    beneficiary_key = config.BENEFICIARY_PRIVATE_KEY
    beneficiary = c.w3.eth.account.from_key(beneficiary_key).address
    owner = c.w3.eth.account.from_key(owner_key).address

    # Schedule starts now: 1000 tokens, 60s cliff, 600s linear, revocable.
    total = 1_000 * ONE
    start = c.w3.eth.get_block("latest")["timestamp"]
    res = c.create_schedule(
        owner_key=owner_key,
        beneficiary=beneficiary,
        amount=total,
        start_timestamp=start,
        cliff_duration=60,
        vesting_duration=600,
        revocable=True,
    )
    sid = res["schedule_id"]
    print(f"created schedule #{sid}: {total // ONE} tokens, cliff 60s, linear 600s")

    # Try to claim during the cliff -> revert.
    try:
        c.release(beneficiary_key, sid)
    except Exception as exc:
        print(f"claim during cliff rejected: {str(exc).splitlines()[-1][:80]}")

    # Fast-forward Anvil clock to 300s (half of duration): 50% vested.
    c.pin_next_timestamp(start + 300)
    view = c.get_schedule(sid)
    print(f"t+300s: vested={view.vested_now // ONE} releasable={view.releasable_now // ONE}")
    c.release(beneficiary_key, sid)
    print(f"beneficiary balance after claim: {c.token_balance(beneficiary) // ONE}")

    # Owner revokes: the other 50% (unvested) is refunded immediately.
    owner_before = c.token_balance(owner)
    c.pin_next_timestamp(start + 300)
    c.revoke(owner_key, sid)
    refund = c.token_balance(owner) - owner_before
    print(f"revoked: owner refunded {refund // ONE} tokens")

    # Even far in the future, only the pre-revoke half is claimable.
    c.pin_next_timestamp(start + 10_000)
    view = c.get_schedule(sid)
    remaining = view.total_amount - view.released_amount - view.refunded_amount
    print(f"post-revoke conservation: released={view.released_amount // ONE} "
          f"refunded={view.refunded_amount // ONE} remaining_in_escrow={remaining}")
    assert view.released_amount + view.refunded_amount == total
    print("OK: released + refunded == locked total")


if __name__ == "__main__":
    main()
