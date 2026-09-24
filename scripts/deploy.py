#!/usr/bin/env python3
"""Deploy HashedTimelock to both local Anvil chains and write deployment.json.

Usage:
    # chains already running (anvil on 8545 / 8546):
    python scripts/deploy.py

    # spawn the two anvils, deploy, leave chains running:
    python scripts/deploy.py --spawn
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from web3 import Web3

from htlclib.chains import (
    ALICE_PK,
    CHAIN_A_RPC,
    CHAIN_B_RPC,
    dump_deployment,
    spawn_both,
)
from htlclib.contract import deploy

ROOT = Path(__file__).resolve().parent.parent


def deploy_to(rpc: str, label: str) -> str:
    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 5}))
    if not w3.is_connected():
        raise SystemExit(f"chain {label} not reachable at {rpc}; start anvil first or use --spawn")
    acct = w3.eth.account.from_key(ALICE_PK)
    contract, receipt = deploy(w3, acct.address, ALICE_PK)
    print(f"[chain {label}] rpc={rpc} chain_id={w3.eth.chain_id} "
          f"HashedTimelock={contract.address} block={receipt['blockNumber']}")
    return contract.address


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spawn", action="store_true",
                        help="spawn the two anvil nodes (they keep running)")
    args = parser.parse_args()

    chains = None
    if args.spawn:
        chains = spawn_both(ROOT / "logs")
        time.sleep(0.5)

    try:
        addr_a = deploy_to(CHAIN_A_RPC, "A")
        addr_b = deploy_to(CHAIN_B_RPC, "B")
    except Exception:
        if chains:
            chains[0].stop()
            chains[1].stop()
        raise

    out = ROOT / "deployment.json"
    dump_deployment(out, addr_a, addr_b)
    print(f"wrote {out}")
    if chains:
        print("anvil nodes left running on 8545/8546; Ctrl-C this script to stop them")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            chains[0].stop()
            chains[1].stop()


if __name__ == "__main__":
    main()
