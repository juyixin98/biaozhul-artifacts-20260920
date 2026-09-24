#!/usr/bin/env python3
"""Export contract ABIs + bytecode from Foundry artifacts into backend/app/abi/.

Run after `forge build`:
    python scripts/export_abis.py
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "backend" / "app" / "abi"
CONTRACTS = {
    "MockERC20": ROOT / "out" / "MockERC20.sol" / "MockERC20.json",
    "ShareVault": ROOT / "out" / "ShareVault.sol" / "ShareVault.json",
}


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, artifact in CONTRACTS.items():
        data = json.loads(artifact.read_text())
        exported = {
            "abi": data["abi"],
            "bytecode": data["bytecode"]["object"],
        }
        dest = OUT_DIR / f"{name}.json"
        dest.write_text(json.dumps(exported, indent=2))
        print(f"wrote {dest} ({len(data['abi'])} abi entries)")


if __name__ == "__main__":
    main()
