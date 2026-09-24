#!/usr/bin/env python3
"""Deploy Vault to an already-running Anvil and print its address.

Usage:
    python scripts/deploy.py [RPC_URL] [DEPLOYER_PRIVATE_KEY]

Defaults to the local Anvil endpoint and its first test key. The forge
artifact ``out/Vault.sol/Vault.json`` must exist (run ``forge build`` first).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import onchain  # noqa: E402


def main() -> int:
    rpc = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8545"
    key = sys.argv[2] if len(sys.argv) > 2 else onchain.ANVIL_TEST_KEYS[0]
    w3 = onchain.make_w3(rpc)
    sender = onchain.TxSender(w3)
    addr = onchain.deploy_vault(w3, sender, key)
    print(addr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
