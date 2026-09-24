#!/usr/bin/env python3
"""End-to-end demo: checker + local Anvil chain.

Scenario:
  1. Show the checker verdicts for all five upgrade candidates.
  2. Deploy BoxV1 behind an ERC1967 proxy on Anvil, seed demo data.
  3. Attempt the incompatible BoxV3 upgrade through the API flow (blocked).
  4. Upgrade to BoxV2 (compatible) and prove every datum survived.

Run with Anvil up:  anvil --silent &   python script/demo_upgrade.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from checker import check_layouts
from checker.chain import (
    ANVIL_KEY_0,
    deploy_proxy_with,
    get_web3,
    load_bundle,
    read_demo_data,
    read_implementation,
    seed_demo_data,
    upgrade_proxy,
)

BOLD, DIM, GREEN, RED, RESET = "\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[0m"


def banner(t):
    print(f"\n{BOLD}=== {t} ==={RESET}")


def main() -> int:
    banner("1. Checker verdicts for BoxV1 -> candidate")
    v1 = load_bundle("BoxV1")
    verdicts = {}
    for cand, desc in [
        ("BoxV2", "append-only fields"),
        ("BoxV3", "reordered fields"),
        ("BoxV4", "type widening + dynamic type change"),
        ("BoxV5", "base-class field widened"),
        ("BoxV6", "safe base gap evolution"),
    ]:
        r = check_layouts(v1, load_bundle(cand))
        verdicts[cand] = r.compatible
        mark = f"{GREEN}COMPATIBLE{RESET}" if r.compatible else f"{RED}BLOCKED   {RESET}"
        print(f"  {mark}  BoxV1 -> {cand}  {DIM}({desc}){RESET}")
        for e in r.errors[:3]:
            print(f"           {DIM}· {e.code}: {e.message[:96]}{RESET}")

    banner("2. Deploy BoxV1 + ERC1967 proxy on Anvil")
    w3 = get_web3()
    acct = w3.eth.account.from_key(ANVIL_KEY_0)
    out = deploy_proxy_with(w3, acct, "BoxV1")
    proxy = out["proxy"]
    print(f"  proxy          : {proxy}")
    print(f"  implementation : {out['implementation']}")
    seed_demo_data(w3, acct, proxy, v1["abi"])
    before = read_demo_data(w3, proxy, v1["abi"])
    print(f"  seeded state   : {json.dumps(before)}")

    banner("3. Incompatible upgrade BoxV1 -> BoxV3 (must be blocked)")
    r = check_layouts(v1, load_bundle("BoxV3"))
    print(f"  checker says compatible={r.compatible} -> upgrade REFUSED")
    assert not r.compatible

    banner("4. Compatible upgrade BoxV1 -> BoxV2")
    up = upgrade_proxy(w3, acct, proxy, "BoxV2", v1["abi"])
    v2 = load_bundle("BoxV2")
    after = read_demo_data(w3, proxy, v2["abi"])
    print(f"  new impl       : {up['new_implementation']}")
    print(f"  impl slot now  : {read_implementation(w3, proxy)}")
    preserved = all(before[k] == after[k] for k in before if k in after)
    print(f"  state before   : {json.dumps(before)}")
    print(f"  state after    : {json.dumps(after)}")
    print(f"  {GREEN if preserved else RED}state preserved: {preserved}{RESET}")
    assert preserved
    print(f"\n{GREEN}Demo finished: incompatible upgrade blocked, compatible upgrade preserved all data.{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
