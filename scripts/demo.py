#!/usr/bin/env python3
"""End-to-end demo against a running anvil + deployed contract.

Shows, for one account over 100 blocks:
  * batch settlement (settle once after 100 blocks)
  * per-block settlement (settle every block)
  * both equal the Python big-integer reference exactly
  * the rounding-error bound: 0 <= exact - settled < 1 (in fee-token base units)

Usage:
    python3 scripts/demo.py [account_offset]
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web3 import Web3

from app import chain
from app.reference import RefAccount, SCALE

PRINCIPAL = 1_000_000 * 10**18
RATE = SCALE // 100  # ~1% per block
BLOCKS = 100


def account_state(contract, addr):
    t = contract.functions.getAccount(addr).call()
    limbs, value = chain.fees_limbs_of(t)
    return {"carry": int(t[4]), "last_block": int(t[3]), "fees": value}


def main():
    w3 = chain.get_web3()
    contract = chain.get_contract(w3)
    offset = int(sys.argv[1]) if len(sys.argv) > 1 else 0

    acct_batch = Web3.to_checksum_address(f"0x{0xB000 + offset:040x}")
    acct_perblock = Web3.to_checksum_address(f"0x{0xC000 + offset:040x}")

    # --- batch: open, mine up to h+99, the settle tx's own block is the 100th ---
    chain.send_tx(w3, contract.functions.openAccount(acct_batch, PRINCIPAL, RATE))
    h_batch = account_state(contract, acct_batch)["last_block"]
    chain.mine_blocks(w3, h_batch + BLOCKS - 1 - w3.eth.block_number)
    chain.send_tx(w3, contract.functions.settle(acct_batch))
    batch = account_state(contract, acct_batch)
    print(f"batch   opened at block {h_batch}, settled through {batch['last_block']}: "
          f"fees={batch['fees']} carry={batch['carry']}")

    # --- per-block: open afterwards, then each settle tx is its own block ---
    chain.send_tx(w3, contract.functions.openAccount(acct_perblock, PRINCIPAL, RATE))
    h_per = account_state(contract, acct_perblock)["last_block"]
    for _ in range(BLOCKS):
        chain.send_tx(w3, contract.functions.settle(acct_perblock))
    perblock = account_state(contract, acct_perblock)
    print(f"per-blk opened at block {h_per}, settled through {perblock['last_block']}: "
          f"fees={perblock['fees']} carry={perblock['carry']}")

    # --- reference ---
    ref = RefAccount(PRINCIPAL, RATE, 0)
    ref.settle(BLOCKS)
    print(f"python  reference over {BLOCKS} blocks:      fees={ref.fees} carry={ref.carry}")

    assert batch["last_block"] == h_batch + BLOCKS, "batch span != 100 blocks"
    assert perblock["last_block"] == h_per + BLOCKS, "per-block span != 100 blocks"
    assert (batch["fees"], batch["carry"]) == (perblock["fees"], perblock["carry"]), \
        "batch != per-block"
    assert batch["fees"] == ref.fees and batch["carry"] == ref.carry, "on-chain != reference"

    exact_num = BLOCKS * PRINCIPAL * RATE  # exact fee = exact_num / SCALE
    settled, rem = divmod(exact_num, SCALE)
    assert rem == batch["carry"] and settled == batch["fees"]
    print(f"\nexact fee   = {exact_num}/{SCALE} = {settled} + {rem}/{SCALE}")
    print(f"settled fee = {batch['fees']}  (rounding error {rem}/{SCALE} < 1 base unit, carried)")
    print("\nOK: batch == per-block == reference; no dust lost, no double counting.")


if __name__ == "__main__":
    main()
