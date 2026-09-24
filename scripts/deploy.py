#!/usr/bin/env python3
"""Deploy FeeSettlement to the local anvil chain and write deployment.json.

Usage:
    python3 scripts/deploy.py            # uses RPC_URL / PRIVATE_KEY env or anvil defaults
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import chain, config


def main():
    w3 = chain.get_web3()
    address = chain.deploy_contract(w3)
    info = {
        "address": address,
        "chain_id": w3.eth.chain_id,
        "rpc_url": config.RPC_URL,
        "deployer": chain.signer_address(w3),
    }
    with open(config.DEPLOYMENT_FILE, "w") as f:
        json.dump(info, f, indent=2)
    print(f"FeeSettlement deployed at {address} (chain {info['chain_id']})")
    print(f"written to {config.DEPLOYMENT_FILE}")


if __name__ == "__main__":
    main()
