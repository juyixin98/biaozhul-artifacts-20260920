"""Deploy MultisigTimelock + demo targets to a local Anvil chain.

Usage:
    anvil --chain-id 31337
    python -m service.deploy
    python -m service.deploy --timelock 2 --max-failures 2 --retry-cooldown 1

Writes deployments.json consumed by the API service.
"""

from __future__ import annotations

import argparse
import json

from eth_account import Account

from .chain import ChainClient
from .config import ANVIL_KEYS, DEPLOYMENTS_FILE, Settings


def deploy_all(settings: Settings, signer_addresses: list[str], threshold: int,
               timelock: int, max_failures: int, retry_cooldown: int) -> dict:
    chain = ChainClient(settings.rpc_url, settings.chain_id)
    deployer = settings.operator_key

    executor_addr, _ = chain.deploy(
        "MultisigTimelock",
        [signer_addresses, threshold, timelock, max_failures, retry_cooldown],
        deployer,
    )
    counter_addr, _ = chain.deploy("Counter", [], deployer)
    flaky_addr, _ = chain.deploy("FlakyTarget", [], deployer)
    always_fail_addr, _ = chain.deploy("AlwaysFail", [], deployer)
    reentrant_addr, _ = chain.deploy(
        "ReentrantTarget", [executor_addr], deployer
    )

    return {
        "rpc_url": settings.rpc_url,
        "chain_id": chain.chain_id,
        "executor": executor_addr,
        "signers": signer_addresses,
        "threshold": threshold,
        "params": {
            "timelock_seconds": timelock,
            "max_failures": max_failures,
            "retry_cooldown_seconds": retry_cooldown,
        },
        "targets": {
            "counter": counter_addr,
            "flaky": flaky_addr,
            "always_fail": always_fail_addr,
            "reentrant": reentrant_addr,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--threshold", type=int, default=2)
    parser.add_argument("--timelock", type=int, default=2)
    parser.add_argument("--max-failures", type=int, default=2)
    parser.add_argument("--retry-cooldown", type=int, default=1)
    parser.add_argument("--signer-count", type=int, default=3)
    args = parser.parse_args()

    settings = Settings()
    signer_addresses = [
        Account.from_key(k).address for k in ANVIL_KEYS[: args.signer_count]
    ]
    if not (1 <= args.threshold <= len(signer_addresses)):
        raise SystemExit(
            f"threshold {args.threshold} must be in 1..{len(signer_addresses)}"
        )

    deployment = deploy_all(
        settings,
        signer_addresses,
        args.threshold,
        args.timelock,
        args.max_failures,
        args.retry_cooldown,
    )
    out = settings.deployments_path
    out.write_text(json.dumps(deployment, indent=2) + "\n", encoding="utf-8")
    print(f"deployed to {settings.rpc_url} (chain {settings.chain_id})")
    print(f"  executor:    {deployment['executor']}")
    for name, addr in deployment["targets"].items():
        print(f"  {name:<11} {addr}")
    print(f"  signers:     {signer_addresses} (threshold {args.threshold})")
    print(f"  timelock:    {args.timelock}s, cooldown: "
          f"{args.retry_cooldown}s, max failures: {args.max_failures}")
    print(f"deployment written to {out}")


if __name__ == "__main__":
    main()
