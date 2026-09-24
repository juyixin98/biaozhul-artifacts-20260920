#!/usr/bin/env python3
"""End-to-end walkthrough against a running local Anvil + deployed contracts.

Demonstrates: cliff, linear vesting, repeated claims, revocation interleaved
with claims, non-divisible total, and the conservation identity
    final_claimed + owner_refund == initial_locked.

Prereqs:
    anvil            # terminal 1, default http://127.0.0.1:8545
    forge build && python scripts/deploy.py
    python scripts/demo.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.chain import (  # noqa: E402
    account_from_key,
    deployed_handles,
    get_web3,
)

TOK = 10**18
# Anvil account #1 as beneficiary.
BENEFICIARY_KEY = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"


def travel(w3, ts: int) -> None:
    w3.provider.make_request("evm_setTime", [int(ts)])  # Anvil 1.8: seconds
    w3.provider.make_request("evm_mine", [])


def send(w3, account, func):
    tx = func.build_transaction(
        {
            "from": account.address,
            "nonce": w3.eth.get_transaction_count(account.address),
            "gas": int(func.estimate_gas({"from": account.address}) * 1.25),
            "gasPrice": w3.eth.gas_price,
            "chainId": w3.eth.chain_id,
        }
    )
    signed = account.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
    h = w3.eth.send_raw_transaction(raw)
    rcpt = w3.eth.wait_for_transaction_receipt(h)
    if int(rcpt.status) != 1:
        raise RuntimeError(f"tx reverted: {h.hex()}")
    return h.hex()


def main() -> int:
    w3 = get_web3()
    token, vesting = deployed_handles()[1], deployed_handles()[2]
    owner = account_from_key(w3)
    beneficiary = w3.eth.account.from_key(BENEFICIARY_KEY)

    print(f"chain id     : {w3.eth.chain_id}")
    print(f"owner        : {owner.address}")
    print(f"beneficiary  : {beneficiary.address}")
    print(f"token        : {token.address}")
    print(f"vesting      : {vesting.address}")

    # odd, non-divisible total
    amount = 12345 * TOK + 678
    start = 1_000_000
    cliff = start + 50
    end = start + 500

    send(w3, owner, token.functions.approve(vesting.address, 2**256 - 1))
    send(
        w3,
        owner,
        vesting.functions.createSchedule(
            beneficiary.address, token.address, amount, start, cliff, end
        ),
    )
    sid = int(vesting.functions.nextScheduleId().call())
    print(f"\ncreated schedule #{sid}: locked={amount / TOK:.6f} SYN")
    print(f"  start={start} cliff={cliff} end={end}")

    owner_bal0 = int(token.functions.balanceOf(owner.address).call())

    travel(w3, start + 60)
    send(w3, owner, vesting.functions.release(sid))
    c1 = int(token.functions.balanceOf(beneficiary.address).call())
    print(f"\n@t+60  claim 1 -> beneficiary={c1 / TOK:.6f}")

    travel(w3, start + 300)
    send(w3, owner, vesting.functions.release(sid))
    c2 = int(token.functions.balanceOf(beneficiary.address).call())
    print(f"@t+300 claim 2 -> beneficiary cumulative={c2 / TOK:.6f}")

    revoke_t = start + 400
    travel(w3, revoke_t)
    send(w3, owner, vesting.functions.revoke(sid))
    vested_final = amount * (revoke_t - start) // (end - start)
    print(f"@t+400 REVOKED -> vested frozen={vested_final / TOK:.6f}")

    travel(w3, end + 1000)
    send(w3, owner, vesting.functions.release(sid))
    claimed_total = int(token.functions.balanceOf(beneficiary.address).call())
    refund = int(token.functions.balanceOf(owner.address).call()) - owner_bal0
    escrow = int(token.functions.balanceOf(vesting.address).call())

    print("\n--- final settlement ---")
    print(f"beneficiary claimed : {claimed_total / TOK:.6f}")
    print(f"owner refund        : {refund / TOK:.6f}")
    print(f"escrow remainder    : {escrow / TOK:.6f}")
    print(f"claimed + refund    : {(claimed_total + refund) / TOK:.6f}")
    print(f"initial locked      : {amount / TOK:.6f}")

    ok = claimed_total + refund == amount and escrow == 0
    print("\nCONSERVATION CHECK:", "PASS ✅" if ok else "FAIL ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
