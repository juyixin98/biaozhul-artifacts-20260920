#!/usr/bin/env python3
"""End-to-end demo against two ephemeral local Anvil chains.

Spawns both chains, deploys the contracts, and runs three scenarios:
  1. happy swap      — both legs CLAIMED with one revealed preimage
  2. safe abort      — one chain briefly halts before reveal; both refund
  3. timeout-gap loss— reveal + halt longer than DELTA; split outcome

Results are written to demo-results.json and printed as JSON. The script never
connects to anything except 127.0.0.1.

Usage:
    python scripts/demo.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from htlclib.harness import build_env, run_happy_swap, run_safe_abort, run_timeout_gap_loss

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    env = build_env(log_dir=ROOT / "logs", deployment_file=ROOT / "deployment.json")
    results: dict[str, object] = {
        "chains": {
            "A": {"rpc": env.chain_a.rpc, "chain_id": env.chain_a.chain_id,
                  "contract": env.cli_a.address},
            "B": {"rpc": env.chain_b.rpc, "chain_id": env.chain_b.chain_id,
                  "contract": env.cli_b.address},
        }
    }
    try:
        print("== scenario 1: happy swap ==")
        results["happy_swap"] = run_happy_swap(env)
        print(json.dumps(results["happy_swap"], indent=2, default=str))

        print("== scenario 2: one-chain halt (3s < DELTA=20s), safe abort ==")
        results["safe_abort"] = run_safe_abort(env, halt_seconds=3)
        print(json.dumps(results["safe_abort"], indent=2, default=str))

        print("== scenario 3: reveal then halt > DELTA, timeout-gap loss ==")
        results["timeout_gap_loss"] = run_timeout_gap_loss(env)
        print(json.dumps(results["timeout_gap_loss"], indent=2, default=str))
    finally:
        out = ROOT / "demo-results.json"
        out.write_text(json.dumps(results, indent=2, default=str) + "\n")
        env.stop()
        print(f"results written to {out}")


if __name__ == "__main__":
    main()
