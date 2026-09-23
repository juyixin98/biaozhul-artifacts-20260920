#!/usr/bin/env python3
"""Generate deterministic state-sequence fixtures consumed by the Solidity
test ``Sequence.t.sol``.

Usage:
    python3 reference/scenarios.py            # writes reference/fixtures/scenarios.json
    python3 reference/scenarios.py --check    # self-tests only, no write

Each scenario is an ordered list of steps. Every state-mutating step records:
  * the resulting on-chain state (reserves == balances, supplies, user balances)
  * the real-arithmetic reference value, so the integer model's rounding loss
    can be bounded independently
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(__file__))
from cpmm import (  # noqa: E402
    CPMMPoolModel, ModelRevert, evm_get_amount_out, real_get_amount_out,
    MINIMUM_LIQUIDITY,
)

WAD = 10**18

# ---------------------------------------------------------------------------
# Scenario definitions
# ---------------------------------------------------------------------------

def scenario_bootstrap_and_trade() -> dict:
    """The main happy-path state sequence."""
    m = CPMMPoolModel()
    steps: list[dict] = []
    m.fund("alice", 200 * WAD, 200 * WAD)
    m.fund("bob", 1_000 * WAD, 500 * WAD)

    steps.append(_step("fund", {"user": "alice", "amount0": 200 * WAD,
                                "amount1": 200 * WAD, "bob0": 1_000 * WAD,
                                "bob1": 500 * WAD}))
    steps[-1]["state"] = m.snapshot()

    r = m.deposit("alice", 100 * WAD, 100 * WAD)
    steps.append(_step("deposit", {"user": "alice", "amount0": 100 * WAD,
                                   "amount1": 100 * WAD}, r, m))

    # Bob adds asymmetric liquidity at the 1:1 ratio.
    r = m.deposit("bob", 200 * WAD, 200 * WAD)
    steps.append(_step("deposit", {"user": "bob", "amount0": 200 * WAD,
                                   "amount1": 200 * WAD}, r, m))

    # Classic 0.3% trade: 10 tokens in against 300/300 reserves -> 9 out.
    r = m.swap("bob", "token0", 10 * WAD)
    steps.append(_step("swap", {"user": "bob", "tokenIn": "token0",
                                "amountIn": 10 * WAD}, r, m))

    # Reverse swap of 1 token1.
    r = m.swap("alice", "token1", WAD)
    steps.append(_step("swap", {"user": "alice", "tokenIn": "token1",
                                "amountIn": WAD}, r, m))

    # Alice removes 1/3 of her shares (floor math).
    alice_lp = m.user("alice").lp
    burn_liq = alice_lp // 3
    r = m.burn("alice", burn_liq)
    steps.append(_step("burn", {"user": "alice", "liquidity": burn_liq}, r, m))

    # Larger trade then partial burn by Bob, then burn the remainder.
    r = m.swap("bob", "token0", 50 * WAD)
    steps.append(_step("swap", {"user": "bob", "tokenIn": "token0",
                                "amountIn": 50 * WAD}, r, m))
    half = m.user("bob").lp // 2
    r = m.burn("bob", half)
    steps.append(_step("burn", {"user": "bob", "liquidity": half}, r, m))
    rest = m.user("bob").lp
    r = m.burn("bob", rest)
    steps.append(_step("burn", {"user": "bob", "liquidity": rest}, r, m))

    return {"name": "bootstrap_and_trade",
            "description": "bootstrap, asymmetric deposits, trades both "
                           "directions, partial and full burns",
            "steps": steps}


def scenario_dust_fee() -> dict:
    """Tiny inputs against large reserves: rounding must never gift output
    beyond the fee-adjusted real curve, and zero-output swaps revert."""
    m = CPMMPoolModel()
    steps: list[dict] = []
    m.fund("alice", 10**24, 10**24)
    m.fund("bob", 10**13, 10**13)
    steps.append(_step("fund", {"user": "alice", "amount0": 10**24,
                                "amount1": 10**24, "bob0": 10**13,
                                "bob1": 10**13}))
    steps[-1]["state"] = m.snapshot()
    r = m.deposit("alice", 10**24, 10**24)
    steps.append(_step("deposit", {"user": "alice", "amount0": 10**24,
                                   "amount1": 10**24}, r, m))

    # Dust swaps. Many of these round to zero and must revert atomically.
    dust_inputs = [1, 10, 100, 1_000, 10_000, 10**6, 10**9, 10**12]
    for i, ain in enumerate(dust_inputs):
        real_out = real_get_amount_out(ain, 10**24, 10**24)
        evm_out = evm_get_amount_out(ain, 10**24, 10**24)
        if evm_out == 0:
            steps.append({
                "op": "swap",
                "user": "bob",
                "tokenIn": "token0",
                "amountIn": str(ain),
                "expectRevert": "CPMM: INSUFFICIENT_OUTPUT_AMOUNT (rounds to 0)",
                "realAmountOut": str(real_out),
                "evmAmountOut": "0",
            })
        else:
            r = m.swap("bob", "token0", ain)
            s = _step("swap", {"user": "bob", "tokenIn": "token0",
                               "amountIn": ain}, r, m)
            s["realAmountOut"] = str(real_out)
            s["evmAmountOut"] = str(evm_out)
            steps.append(s)

    # Compare against a *zero-fee* curve: the fee must never be bypassed.
    # no-fee output = ain*Rout/(Rin+ain); with a fee, output <= no-fee.
    for ain in (WAD, 777 * WAD, 31_337):
        paid = evm_get_amount_out(ain, m.pool.reserve0, m.pool.reserve1)
        no_fee = (ain * m.pool.reserve1) // (m.pool.reserve0 + ain)
        assert paid <= no_fee
        steps.append({
            "op": "feeBound",
            "amountIn": str(ain),
            "withFeeOut": str(paid),
            "zeroFeeOut": str(no_fee),
        })

    return {"name": "dust_fee",
            "description": "dust inputs cannot bypass the 30 bps fee; "
                           "zero-output swaps revert",
            "steps": steps}


def scenario_micro_bootstrap() -> dict:
    """Deposits near the MINIMUM_LIQUIDITY floor, plus a second deposit into
    a *skewed* pool that rounds the LP share down to zero (must revert)."""
    m = CPMMPoolModel()
    steps: list[dict] = []
    # Alice bootstraps a heavily skewed pool (ratio 1e12:1).
    m.fund("alice", 10**18, 10**6)
    m.fund("bob", 10**18, 10**18)
    steps.append(_step("fund", {"user": "alice", "amount0": 10**18,
                                "amount1": 10**6, "bob0": 10**18,
                                "bob1": 10**18}))
    steps[-1]["state"] = m.snapshot()

    # A balanced micro-pool rejections are checked on a throwaway model path
    # below via direct integer assertions, so here we bootstrap the skewed one.
    r = m.deposit("alice", 10**18, 10**6)
    # sqrt(1e18*1e6) = 1e12, minus the 1_000 lock.
    assert r["liquidity"] == str(10**12 - 1_000), r["liquidity"]
    steps.append(_step("deposit", {"user": "alice", "amount0": 10**18,
                                   "amount1": 10**6}, r, m))

    # Bob deposits 1 wei of the abundant token0. LP = 1*1e12/1e18 = 0 -> revert.
    steps.append({
        "op": "deposit", "user": "bob",
        "amount0": "1", "amount1": "1",
        "expectRevert": "CPMM: INSUFFICIENT_FIRST_LIQUIDITY",
    })
    # 999_999 token0 still rounds to zero (999999*1e12/1e18 < 1).
    steps.append({
        "op": "deposit", "user": "bob",
        "amount0": str(10**6 - 1), "amount1": "1",
        "expectRevert": "CPMM: INSUFFICIENT_FIRST_LIQUIDITY",
    })
    # Boundary: 1_000_001 token0 is the first amount that yields 1 share.
    r = m.deposit("bob", 10**6 + 1, 1)
    assert r["liquidity"] == "1", r["liquidity"]
    steps.append(_step("deposit", {"user": "bob", "amount0": 10**6 + 1,
                                   "amount1": 1}, r, m))

    # Direct integer checks for the balanced micro bootstrap floor.
    from math import isqrt
    assert isqrt(MINIMUM_LIQUIDITY * MINIMUM_LIQUIDITY) - MINIMUM_LIQUIDITY == 0
    assert isqrt(1001 * 1000) - MINIMUM_LIQUIDITY == 0
    assert isqrt(10**6 * 10**6) - MINIMUM_LIQUIDITY == 999_000
    return {"name": "micro_bootstrap",
            "description": "minimum liquidity lock; second deposits that "
                           "round to zero shares revert; balanced-floor math "
                           "checked as integer assertions",
            "steps": steps}


def scenario_full_withdrawal() -> dict:
    """After all circulating shares are burned, reserves only cover the
    permanently locked 1_000-share tail, proving it can never be withdrawn."""
    m = CPMMPoolModel()
    steps: list[dict] = []
    m.fund("alice", 400 * WAD, 900 * WAD)
    m.fund("bob", 10**9, 10**9)
    steps.append(_step("fund", {"user": "alice", "amount0": 400 * WAD,
                                "amount1": 900 * WAD, "bob0": 10**9,
                                "bob1": 10**9}))
    steps[-1]["state"] = m.snapshot()

    r = m.deposit("alice", 400 * WAD, 900 * WAD)
    steps.append(_step("deposit", {"user": "alice", "amount0": 400 * WAD,
                                   "amount1": 900 * WAD}, r, m))

    r = m.swap("bob", "token0", 10**9)  # bob barely affects reserves
    steps.append(_step("swap", {"user": "bob", "tokenIn": "token0",
                                "amountIn": 10**9}, r, m))

    # Alice burns everything she owns.
    alice_lp = m.user("alice").lp
    r = m.burn("alice", alice_lp)
    steps.append(_step("burn", {"user": "alice", "liquidity": alice_lp}, r, m))

    assert m.user("alice").lp == 0
    # Pool still holds the value attributable to the locked 1_000 shares.
    assert m.pool.total_supply == MINIMUM_LIQUIDITY
    assert m.pool.balance0 > 0 and m.pool.balance1 > 0
    return {"name": "full_withdrawal",
            "description": "100% circulating burn still leaves the locked "
                           "MINIMUM_LIQUIDITY value in the pool",
            "steps": steps}


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------

def _step(op: str, args: dict, result: dict | None = None,
          m: CPMMPoolModel | None = None) -> dict:
    s = {"op": op, **{k: (str(v) if isinstance(v, int) else v)
                      for k, v in args.items()}}
    if result is not None:
        s.update(result)
    if m is not None:
        s["state"] = m.snapshot()
    return s


def build() -> list[dict]:
    return [
        scenario_bootstrap_and_trade(),
        scenario_dust_fee(),
        scenario_micro_bootstrap(),
        scenario_full_withdrawal(),
    ]


def self_check(scenarios: list[dict]) -> None:
    """Cross-check the integer model against the Decimal real model."""
    # 1. Integer floor output is within [real-1, real] for a wide range.
    for ain in range(1, 50):
        for (ri, ro) in ((10**6, 10**6), (10**18, 10**21), (10**24, 10**9)):
            got = evm_get_amount_out(ain, ri, ro)
            real = real_get_amount_out(ain, ri, ro)
            assert got <= int(real.to_integral_value(rounding="ROUND_FLOOR")) + 1
            assert Decimal(got) <= real
            assert real - Decimal(got) < Decimal(1)

    # 2. Fee inequality: output(997) <= output(no fee), strictly less when
    #    the fee terms survive rounding.
    for ain in (1, 10**18, 10**24):
        paid = evm_get_amount_out(ain, 10**24, 10**24)
        free = (ain * 10**24) // (10**24 + ain)
        assert paid <= free

    # 3. Every recorded state keeps reserves == balances.
    for sc in scenarios:
        for st in sc["steps"]:
            if "state" in st:
                s = st["state"]
                assert s["reserve0"] == s["bal0"], sc["name"]
                assert s["reserve1"] == s["bal1"], sc["name"]
    print(f"self-check OK across {len(scenarios)} scenarios")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--out", default=os.path.join(
        os.path.dirname(__file__), "fixtures", "scenarios.json"))
    args = ap.parse_args()

    scenarios = build()
    self_check(scenarios)

    if not args.check:
        os.makedirs(os.path.dirname(args.out), exist_ok=True)
        payload = {
            "scenarioCount": len(scenarios),
            "scenarios": [
                {**s, "stepCount": len(s["steps"])} for s in scenarios
            ],
        }
        with open(args.out, "w") as f:
            json.dump(payload, f, indent=2)
            f.write("\n")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
