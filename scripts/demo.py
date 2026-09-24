#!/usr/bin/env python3
"""Live demo of the acceptance criterion on a local Anvil chain.

Boots Anvil + the FastAPI service, then shows that settling ONCE across
100 blocks and settling EVERY block produce identical fees and identical
carried remainder — i.e. no dust is ever lost to rounding.

Usage:
    python scripts/demo.py            # boots its own anvil + uvicorn
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import requests
from web3 import Web3

ROOT = Path(__file__).resolve().parent.parent
ARTIFACT = ROOT / "out" / "LosslessFeeSettlement.sol" / "LosslessFeeSettlement.json"
KEY_A = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
KEY_B = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
SCALE = 10**18


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> None:
    anvil = shutil.which("anvil") or str(Path.home() / ".foundry/bin/anvil")
    anvil_port, api_port = free_port(), free_port()
    procs = [
        subprocess.Popen([anvil, "--port", str(anvil_port), "--silent"]),
        subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(api_port)],
            cwd=ROOT,
            env=dict(os.environ, RPC_URL=f"http://127.0.0.1:{anvil_port}", PRIVATE_KEY=KEY_A),
        ),
    ]
    base = f"http://127.0.0.1:{api_port}"
    try:
        for _ in range(150):
            try:
                h = requests.get(base + "/health", timeout=1).json()
                if h["connected"] and h["contract"]:
                    break
            except Exception:
                time.sleep(0.2)
        print(f"contract: {h['contract']}  operator: {h['operator']}")
        operator = h["operator"]

        w3 = Web3(Web3.HTTPProvider(f"http://127.0.0.1:{anvil_port}"))
        artifact = json.loads(ARTIFACT.read_text())
        c = w3.eth.contract(address=h["contract"], abi=artifact["abi"])
        acct_b = w3.eth.account.from_key(KEY_B)

        def send_b(fn):
            tx = fn.build_transaction(
                {
                    "from": acct_b.address,
                    "nonce": w3.eth.get_transaction_count(acct_b.address),
                    "gas": 3_000_000,
                }
            )
            rcpt = w3.eth.wait_for_transaction_receipt(
                w3.eth.send_raw_transaction(acct_b.sign_transaction(tx).raw_transaction)
            )
            assert rcpt["status"] == 1

        p, rate = 1_000_003, 3 * 10**15
        print(f"\nprincipal = {p} base units, rate = 0.3%/block (dust-producing)")

        send_b(c.functions.open(p, rate))
        r = requests.post(
            base + "/accounts",
            json={"address": operator, "principal": p, "ratePerBlock": rate},
        )
        r.raise_for_status()

        # B: settle every block (99 settles -> 100 blocks of coverage).
        for _ in range(99):
            send_b(c.functions.settle())
        step_fee, step_rem = (c.functions.getAccount(acct_b.address).call()[i] for i in (3, 4))

        # A: one settle for the same 100-block span.
        r = requests.post(base + "/settle", json={"address": operator})
        r.raise_for_status()
        batch_fee = int(r.json()["feeAdded"])
        acc = requests.get(base + f"/accounts/{operator}").json()

        exact = p * rate * 100
        print(f"\nexact real fee over 100 blocks : {exact / SCALE:.6f} base units")
        print(f"batch settle (1 tx)            : fee={batch_fee}  remainder={acc['remainder']} ({int(acc['remainder']) / SCALE:.4f} of a unit)")
        print(f"stepwise settle (99 txs)       : fee={step_fee}  remainder={step_rem} ({step_rem / SCALE:.4f} of a unit)")
        assert batch_fee == step_fee and int(acc["remainder"]) == step_rem
        assert batch_fee * SCALE + int(acc["remainder"]) == exact
        print("\nOK: identical results; settled fee + carried remainder == exact fee (lossless).")
    finally:
        for proc in procs:
            proc.terminate()
        for proc in procs:
            proc.wait(timeout=10)


if __name__ == "__main__":
    main()
