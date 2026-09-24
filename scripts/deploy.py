#!/usr/bin/env python3
"""Compile (via forge) and deploy SyntheticToken + TokenVesting to a local chain.

Usage:
    forge build
    python scripts/deploy.py [--mint 1000000000000000000000000]

Writes deployment/addresses.json consumed by the API and tests.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from web3 import Web3  # noqa: E402

from app.chain import (  # noqa: E402
    DEPLOYMENT_DIR,
    account_from_key,
    get_contract,
    get_web3,
    load_artifact,
    wait_receipt,
)

ONE_TOK = 10**18


def deploy(w3: Web3, account, name: str, *constructor_args):
    contract = get_contract(w3, name)
    fn = contract.constructor(*constructor_args)
    tx = fn.build_transaction(
        {
            "from": account.address,
            "nonce": w3.eth.get_transaction_count(account.address),
            "gas": int(fn.estimate_gas({"from": account.address}) * 1.25),
            "gasPrice": w3.eth.gas_price,
            "chainId": w3.eth.chain_id,
        }
    )
    signed = account.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
    tx_hash = w3.eth.send_raw_transaction(raw)
    receipt = wait_receipt(w3, tx_hash.hex())
    if int(receipt.status) != 1:
        raise RuntimeError(f"deployment of {name} failed: {receipt}")
    abi = load_artifact(name)["abi"]
    return w3.eth.contract(address=receipt.contractAddress, abi=abi), receipt.contractAddress


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mint", type=int, default=1_000_000 * ONE_TOK,
                        help="initial SYN supply minted to deployer (base units)")
    args = parser.parse_args()

    w3 = get_web3()
    account = account_from_key(w3)
    print(f"connected chainId={w3.eth.chain_id} deployer={account.address}")

    token, token_addr = deploy(w3, account, "SyntheticToken", "Synthetic Token", "SYN", args.mint)
    print(f"SyntheticToken deployed at {token_addr}")

    vesting, vesting_addr = deploy(w3, account, "TokenVesting")
    print(f"TokenVesting  deployed at {vesting_addr}")

    DEPLOYMENT_DIR.mkdir(exist_ok=True)
    out = {
        "chain_id": int(w3.eth.chain_id),
        "token": token_addr,
        "vesting": vesting_addr,
        "deployer": account.address,
        "initial_supply": str(args.mint),
    }
    target = DEPLOYMENT_DIR / "addresses.json"
    target.write_text(json.dumps(out, indent=2) + "\n")
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
