#!/usr/bin/env python3
"""Compile every target contract with the native solc standard-JSON API and
export self-contained layout bundles to ``layouts/<Contract>.json``.

Each bundle contains:
  - contract / source
  - linearizedBaseContracts (AST ids -> names), for inheritance-change checks
  - storageLayout           (solc format: {storage, types})
  - abi / bytecode           (used by the web3 deployer; no forge needed at runtime)

Usage:
    python script/export_layouts.py [--solc PATH] [--out DIR]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

# Contracts that are deployable units whose layouts we compare / deploy.
TARGETS = {
    "src/proxy/ERC1967Proxy.sol": ["ERC1967Proxy"],
    "src/v1/BoxV1.sol": ["BoxV1"],
    "src/v2/BoxV2.sol": ["BoxV2"],
    "src/v3/BoxV3.sol": ["BoxV3"],
    "src/v4/BoxV4.sol": ["BoxV4"],
    "src/v5/BoxV5.sol": ["BoxV5"],
    "src/v6/BoxV6.sol": ["BoxV6"],
}


def find_solc(explicit: str | None) -> str:
    if explicit:
        return explicit
    cands = list(Path.home().glob(".svm/0.8.24/solc-0.8.24")) + list(
        Path.home().glob(".foundry/svm/0.8.24/solc-0.8.24")
    )
    if not cands:
        sys.exit("solc 0.8.24 not found; run `forge build` once or pass --solc")
    return str(cands[0])


def gather_sources() -> dict:
    sources = {}
    for p in SRC.rglob("*.sol"):
        sources[str(p.relative_to(ROOT))] = {"urls": [str(p)]}
    return sources


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--solc", default=None)
    ap.add_argument("--out", default=str(ROOT / "layouts"))
    args = ap.parse_args()

    solc = find_solc(args.solc)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    inp = {
        "language": "Solidity",
        "sources": gather_sources(),
        "settings": {
            "optimizer": {"enabled": True, "runs": 200},
            "evmVersion": "paris",
            "outputSelection": {
                "*": {
                    "*": [
                        "abi",
                        "evm.bytecode.object",
                        "evm.deployedBytecode.object",
                        "storageLayout",
                        "metadata",
                    ],
                    "": ["ast"],
                }
            },
        },
    }

    proc = subprocess.run(
        [solc, "--standard-json", "--allow-paths", str(ROOT)],
        input=json.dumps(inp),
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    out = json.loads(proc.stdout)
    if out.get("errors"):
        fatal = [e for e in out["errors"] if e["severity"] == "error"]
        for e in out["errors"]:
            print(f"{e['severity']}: {e.get('formattedMessage', e.get('message'))}", file=sys.stderr)
        if fatal:
            return 1

    written = []
    # Build a global AST id -> contract-name map (linearization ids span files).
    id_to_name = {}
    name_to_linear_ids = {}
    for src_path, sdata in out["sources"].items():
        for n in sdata.get("ast", {}).get("nodes", []):
            if n.get("nodeType") == "ContractDefinition":
                id_to_name[n["id"]] = n["name"]
                name_to_linear_ids.setdefault(src_path, {})[n["name"]] = n.get(
                    "linearizedBaseContracts", []
                )
    for src, names in TARGETS.items():
        contracts = out["contracts"][src]
        for name in names:
            art = contracts[name]
            lin_ids = name_to_linear_ids.get(src, {}).get(name, [])
            linearized = [id_to_name.get(i, f"<id {i}>") for i in lin_ids]
            bundle = {
                "contract": name,
                "source": src,
                "linearizedBaseContracts": linearized,
                "storageLayout": art["storageLayout"],
                "abi": art["abi"],
                "bytecode": art["evm"]["bytecode"]["object"],
                "deployedBytecode": art["evm"]["deployedBytecode"]["object"],
            }
            dest = out_dir / f"{name}.json"
            dest.write_text(json.dumps(bundle, indent=2))
            written.append((name, str(dest.relative_to(ROOT))))

    for name, path in written:
        print(f"exported {name:14s} -> {path}")
    print(f"\nsolc: {solc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
