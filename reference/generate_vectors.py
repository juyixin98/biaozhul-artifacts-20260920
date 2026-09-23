#!/usr/bin/env python3
"""Generate the replayable reference-model vectors consumed by Solidity tests.

Outputs:
  test/vectors/vectors.json      machine-readable vectors (ReferenceVectors.t.sol)
  test/vectors/manifest.json     generator metadata / hashes / precision checks
  examples/sample-sequence.json  one hand-readable lifecycle + its transcript
  examples/precision-report.txt  integer-vs-continuous gap summary

Design:
  * seeded RNG -> fully deterministic and regeneratable
  * every successful arithmetic result is cross-checked against the 80-digit
    continuous model and the floor gap 0 <= real - floor < 1 is asserted
  * every lifecycle case asserts conservation (tokens and shares), the
    locked shares, and post-swap k growth in the GENERATOR, while Solidity
    replays the same sequences and asserts the identical resulting state
"""
from __future__ import annotations

import hashlib
import json
import os
import random
from decimal import Decimal

from cpmm_ref import (
    FEE_DENOMINATOR,
    FEE_NUMERATOR,
    MINIMUM_LIQUIDITY,
    RefError,
    RefRevert,
    ReferencePool,
    continuous_amount_out,
    continuous_burn_amounts,
    continuous_mint_shares,
    effective_fee_bps,
    floor_gap,
    integer_amount_out,
    integer_burn_amounts,
    integer_deposit_amounts,
    integer_mint_shares,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VEC_DIR = os.path.join(ROOT, "test", "vectors")
EX_DIR = os.path.join(ROOT, "examples")
SEED = 0x1337
N_CASES = 30

# Named actors -> addresses the Solidity harness deploys/pranks. Addresses
# are arbitrary deterministic hex; the harness labels balances by id.
ACTORS = ["alice", "bob", "carol", "dave"]
ACTOR_ADDR = {
    "alice": "0xA11CE00000000000000000000000000000000001",
    "bob": "0xB0B0000000000000000000000000000000000002",
    "carol": "0xC0A0000000000000000000000000000000000003",
    "dave": "0xDA7E000000000000000000000000000000000004",
    "LP": "0x1000000000000000000000000000000000000001",
    "SWAP": "0x2000000000000000000000000000000000000002",
}

# ---------------------------------------------------------------------
# precision checks (integer result vs 80-digit continuous model)
# ---------------------------------------------------------------------
precision_rows: list[dict] = []
max_gap = Decimal(0)


def record_gap(label: str, real: Decimal, floored: int, bound: str = "1",
               reserves: tuple[int, int, int] | None = None) -> None:
    """Assert the floor gap is within its proven analytic bound.

    bound "1": one nested floor (mint/burn): 0 <= real - floor < 1.
    bound "2": swap, two nested floors. With a = in*997/1000 (exact),
               b = floor(a), R=rOut, r=rIn:
                 real   = a*R/(r+a)
                 floor1 = b*R/(r+b)
                 gap = real - floor(floor1)
                     < (real - floor1) + 1
                     = R*r*(a-b)/((r+a)(r+b)) + 1
                     < R*r/(r+b)^2 + 1
               The last term is evaluated per-swap from the actual reserves,
               so the check stays tight even for heavily skewed pools.
    """
    global max_gap
    gap = floor_gap(real, floored)
    if bound == "2":
        assert reserves is not None
        r_in, r_out, b = reserves
        limit = Decimal(1) + Decimal(r_out) * Decimal(r_in) / (
            Decimal(r_in) + Decimal(b)
        ) ** 2
    else:
        limit = Decimal(1)
    assert Decimal(0) <= gap < limit, f"floor gap out of range: {label} {gap} >= {limit}"
    max_gap = max(max_gap, gap)
    precision_rows.append(
        {
            "op": label,
            "real": format(real, "f"),
            "integer": floored,
            "floorGap": format(gap, "f"),
        }
    )


def assert_conservation(case: dict, pool: ReferencePool, init_tot: tuple[int, int]) -> None:
    """Funded token supply is conserved: sum(account balances) + reserves.

    Deposits move tokens into the pool, withdrawals/swaps move them back out;
    the model never mints or burns assets, so the totals must equal the
    initially funded amounts exactly.
    """
    tot0 = sum(a.bal0 for a in pool.accounts.values()) + pool.r0
    tot1 = sum(a.bal1 for a in pool.accounts.values()) + pool.r1
    i0, i1 = init_tot
    assert tot0 == i0, f"{case['name']}: token0 leak {tot0} != {i0}"
    assert tot1 == i1, f"{case['name']}: token1 leak {tot1} != {i1}"
    held = sum(a.shares for a in pool.accounts.values())
    assert held == pool.total_shares, f"{case['name']}: share leak"
    locked = pool.accounts[pool.LOCK].shares
    # Lock is 0 before the first successful mint and exactly
    # MINIMUM_LIQUIDITY forever after; it can never be anything else.
    assert locked in (0, MINIMUM_LIQUIDITY), f"{case['name']}: bad lock {locked}"
    if pool.total_shares != 0:
        assert locked == MINIMUM_LIQUIDITY, f"{case['name']}: lock missing after mint"


# ---------------------------------------------------------------------
# case / step builders
# ---------------------------------------------------------------------
def fund(actor: str, b0: int, b1: int) -> dict:
    return {"op": "fund", "actor": actor, "amount0": b0, "amount1": b1}


def snap(pool: ReferencePool) -> dict:
    return pool.snapshot()


# Canonical step layout: every key is always present so Solidity can decode
# each record as one fixed tuple via `vm.parseJson*`.
def normalize_step(st: dict) -> dict:
    return {
        "op": st["op"],
        "actor": st.get("actor", "alice"),
        "to": st.get("to", st.get("actor", "alice")),
        "tokenIn": int(st.get("tokenIn", 0)),
        "amountIn": int(st.get("amountIn", 0)),
        "amount0Desired": int(st.get("amount0Desired", 0)),
        "amount1Desired": int(st.get("amount1Desired", 0)),
        "shares": int(st.get("shares", 0)),
        "minShares": int(st.get("minShares", 0)),
        "minAmount0": int(st.get("minAmount0", 0)),
        "minAmount1": int(st.get("minAmount1", 0)),
        "minAmountOut": int(st.get("minAmountOut", 0)),
        "deadline": int(st["deadline"]),
    }


def run_step(pool: ReferencePool, step: dict, now: int) -> dict:
    """Apply one step to the model; attach expected outcome + post-state."""
    op = step["op"]
    # fixed-shape result record
    out = {
        "step": normalize_step(step),
        "time": now,
        "ok": False,
        "revert": "",
        "ret": {"amount0": 0, "amount1": 0, "shares": 0,
                "amountIn": 0, "amountOut": 0},
    }
    try:
        if op == "addLiquidity":
            old_r0, old_r1, old_ts = pool.r0, pool.r1, pool.total_shares
            r = pool.add_liquidity(
                step["actor"],
                step["amount0Desired"],
                step["amount1Desired"],
                step["minShares"],
                step["deadline"],
                now,
            )
            out["ok"] = True
            out["ret"] = {"amount0": r.amount0, "amount1": r.amount1,
                          "shares": r.shares, "amountIn": 0, "amountOut": 0}
            if old_ts != 0:
                real = continuous_mint_shares(
                    r.amount0, r.amount1, old_r0, old_r1, old_ts,
                )
                record_gap("mint", real, r.shares)
        elif op == "removeLiquidity":
            r = pool.remove_liquidity(
                step["actor"],
                step["shares"],
                step["minAmount0"],
                step["minAmount1"],
                step.get("to", step["actor"]),
                step["deadline"],
                now,
            )
            out["ok"] = True
            out["ret"] = {"amount0": r.amount0, "amount1": r.amount1,
                          "shares": r.shares, "amountIn": 0, "amountOut": 0}
            ts_after = pool.total_shares
            a0_real, a1_real = continuous_burn_amounts(
                r.shares,
                pool.r0 + r.amount0,
                pool.r1 + r.amount1,
                ts_after + r.shares,
            )
            record_gap("burn0", a0_real, r.amount0)
            record_gap("burn1", a1_real, r.amount1)
        elif op == "swap":
            r = pool.swap(
                step["actor"],
                step["tokenIn"],
                step["amountIn"],
                step["minAmountOut"],
                step.get("to", step["actor"]),
                step["deadline"],
                now,
            )
            out["ok"] = True
            out["ret"] = {"amount0": 0, "amount1": 0, "shares": 0,
                          "amountIn": r.amount0, "amountOut": r.amount_out}
            rin = pool.r0 - r.amount0 if step["tokenIn"] == 0 else pool.r1 - r.amount0
            rout = pool.r1 + r.amount_out if step["tokenIn"] == 0 else pool.r0 + r.amount_out
            real = continuous_amount_out(r.amount0, rin, rout)
            b_fee = (r.amount0 * FEE_NUMERATOR) // FEE_DENOMINATOR
            record_gap("swap", real, r.amount_out, bound="2",
                       reserves=(rin, rout, b_fee))
            # k must not decrease (fees make it strictly grow except dust).
            assert pool.k() >= rin * rout, "k decreased"
        else:
            raise ValueError(op)
    except RefRevert as e:
        out["revert"] = e.code
    out["state"] = snap(pool)
    return out


def build_case(name: str, steps_seed: list[dict], funds: list[dict]) -> dict:
    rng = random.Random(f"{SEED}:{name}")
    pool = ReferencePool()
    # apply funds
    for f in funds:
        a = pool.acct(f["actor"])
        a.bal0 += f["amount0"]
        a.bal1 += f["amount1"]
    executed: list[dict] = []
    now = 1_000_000
    init_tot = (sum(f["amount0"] for f in funds), sum(f["amount1"] for f in funds))
    for st in steps_seed:
        now += rng.randint(1, 50)
        st = dict(st)
        st.setdefault("deadline", now + 3600)
        st.setdefault("actor", "alice")
        st.setdefault("to", st.get("actor", "alice"))
        if st.get("shares") == "ALL":
            st["shares"] = pool.acct(st["actor"]).shares
        executed.append(run_step(pool, st, now))
    assert_conservation({"name": name}, pool, init_tot)
    used = sorted(
        {f["actor"] for f in funds}
        | {s["step"].get("actor", "alice") for s in executed}
        | {s["step"].get("to", s["step"].get("actor", "alice")) for s in executed}
    )
    return {
        "name": name,
        "actors": used,
        "funds": funds,
        "steps": executed,
        "finalState": snap(pool),
    }


# ---------------------------------------------------------------------
# targeted cases (hand-written, edge semantics)
# ---------------------------------------------------------------------
def targeted_cases() -> list[dict]:
    cases = []
    F = lambda a, x, y: fund(a, x, y)  # noqa: E731

    # 1. first mint: exact share math and 1000-share lock
    cases.append(build_case(
        "first-mint-lock",
        [
            {"op": "addLiquidity", "actor": "LP", "amount0Desired": 10**24,
             "amount1Desired": 10**24, "minShares": 0},
        ],
        [F("LP", 10**24, 10**24)],
    ))

    # 2. dust first mint (<= MINIMUM_LIQUIDITY) rejected
    cases.append(build_case(
        "first-mint-too-small",
        [
            {"op": "addLiquidity", "actor": "LP", "amount0Desired": 999,
             "amount1Desired": 999, "minShares": 0},
        ],
        [F("LP", 10**18, 10**18)],
    ))

    # 3. expired add
    cases.append(build_case(
        "expired-add",
        [
            {"op": "addLiquidity", "actor": "LP", "amount0Desired": 10**24,
             "amount1Desired": 10**24, "minShares": 0, "deadline": 5},
        ],
        [F("LP", 10**24, 10**24)],
    ))

    # 4. expired swap
    cases.append(build_case(
        "expired-swap",
        [
            {"op": "addLiquidity", "actor": "LP", "amount0Desired": 10**24,
             "amount1Desired": 10**24, "minShares": 0},
            {"op": "swap", "actor": "SWAP", "tokenIn": 0, "amountIn": 10**18,
             "minAmountOut": 0, "deadline": 5},
        ],
        [F("LP", 10**24, 10**24), F("SWAP", 10**21, 0)],
    ))

    # 5. dust swap -> floor-fee makes output 0 -> ZeroOutput (no fee bypass)
    # pool 1e24/1e24: inAfterFee = floor(in*997/1000) is 0 for in <= 1;
    # even for in=2/3 the output quotient rounds to 0 as well.
    cases.append(build_case(
        "dust-swap-zero-output",
        [
            {"op": "addLiquidity", "actor": "LP", "amount0Desired": 10**24,
             "amount1Desired": 10**24, "minShares": 0},
            {"op": "swap", "actor": "SWAP", "tokenIn": 0, "amountIn": 1,
             "minAmountOut": 0},
            {"op": "swap", "actor": "SWAP", "tokenIn": 0, "amountIn": 2,
             "minAmountOut": 0},
        ],
        [F("LP", 10**24, 10**24), F("SWAP", 10**9, 0)],
    ))

    # 6. min output slippage
    out = integer_amount_out(10**20, 10**24, 10**24)
    cases.append(build_case(
        "swap-minout-slippage",
        [
            {"op": "addLiquidity", "actor": "LP", "amount0Desired": 10**24,
             "amount1Desired": 10**24, "minShares": 0},
            {"op": "swap", "actor": "SWAP", "tokenIn": 0, "amountIn": 10**20,
             "minAmountOut": out + 1},
            # same swap with honest bound succeeds
            {"op": "swap", "actor": "SWAP", "tokenIn": 0, "amountIn": 10**20,
             "minAmountOut": out},
        ],
        [F("LP", 10**24, 10**24), F("SWAP", 10**21, 0)],
    ))

    # 7. min shares slippage on later deposit
    cases.append(build_case(
        "add-minshares-slippage",
        [
            {"op": "addLiquidity", "actor": "LP", "amount0Desired": 10**24,
             "amount1Desired": 10**24, "minShares": 0},
            {"op": "addLiquidity", "actor": "bob", "amount0Desired": 10**21,
             "amount1Desired": 10**21, "minShares": 10**30},
        ],
        [F("LP", 10**24, 10**24), F("bob", 10**22, 10**22)],
    ))

    # 8. invalid token in
    cases.append(build_case(
        "swap-invalid-token",
        [
            {"op": "addLiquidity", "actor": "LP", "amount0Desired": 10**24,
             "amount1Desired": 10**24, "minShares": 0},
            {"op": "swap", "actor": "SWAP", "tokenIn": 7, "amountIn": 10**18,
             "minAmountOut": 0},
        ],
        [F("LP", 10**24, 10**24), F("SWAP", 10**21, 0)],
    ))

    # 9. remove more shares than owned / zero shares
    cases.append(build_case(
        "remove-overburn-and-zero",
        [
            {"op": "addLiquidity", "actor": "LP", "amount0Desired": 10**24,
             "amount1Desired": 10**24, "minShares": 0},
            {"op": "removeLiquidity", "actor": "bob", "shares": 1,
             "minAmount0": 0, "minAmount1": 0},
            {"op": "removeLiquidity", "actor": "LP", "shares": 0,
             "minAmount0": 0, "minAmount1": 0},
        ],
        [F("LP", 10**24, 10**24)],
    ))

    # 10. remove slippage bounds
    a0, a1 = integer_burn_amounts(10**20, 10**24, 10**24, 10**24)
    cases.append(build_case(
        "remove-slippage",
        [
            {"op": "addLiquidity", "actor": "LP", "amount0Desired": 10**24,
             "amount1Desired": 10**24, "minShares": 0},
            {"op": "removeLiquidity", "actor": "LP", "shares": 10**20,
             "minAmount0": a0 + 1, "minAmount1": a1},
        ],
        [F("LP", 10**24, 10**24)],
    ))

    # 11. underfunded add (one side missing -> transferFrom fails)
    cases.append(build_case(
        "add-underfunded",
        [
            {"op": "addLiquidity", "actor": "LP", "amount0Desired": 10**24,
             "amount1Desired": 10**24, "minShares": 0},
            {"op": "addLiquidity", "actor": "bob", "amount0Desired": 10**21,
             "amount1Desired": 10**21, "minShares": 0},
        ],
        [F("LP", 10**24, 10**24), F("bob", 10**21, 0)],
    ))

    # 12. underfunded swap
    cases.append(build_case(
        "swap-underfunded",
        [
            {"op": "addLiquidity", "actor": "LP", "amount0Desired": 10**24,
             "amount1Desired": 10**24, "minShares": 0},
            {"op": "swap", "actor": "SWAP", "tokenIn": 0, "amountIn": 10**18,
             "minAmountOut": 0},
        ],
        [F("LP", 10**24, 10**24)],  # SWAP has no token0
    ))

    # 13. zero amount inputs
    cases.append(build_case(
        "zero-amount-guard",
        [
            {"op": "addLiquidity", "actor": "LP", "amount0Desired": 0,
             "amount1Desired": 10**24, "minShares": 0},
            {"op": "swap", "actor": "SWAP", "tokenIn": 0, "amountIn": 0,
             "minAmountOut": 0},
        ],
        [F("LP", 10**24, 10**24)],
    ))

    # 14. add/remove preserves ratio for later LP and never touches lock
    cases.append(build_case(
        "proportional-round-trip",
        [
            {"op": "addLiquidity", "actor": "LP", "amount0Desired": 10**24,
             "amount1Desired": 10**24, "minShares": 0},
            {"op": "addLiquidity", "actor": "bob", "amount0Desired": 5 * 10**23,
             "amount1Desired": 5 * 10**23, "minShares": 0},
            {"op": "swap", "actor": "carol", "tokenIn": 0, "amountIn": 3 * 10**20,
             "minAmountOut": 0},
            {"op": "removeLiquidity", "actor": "bob",
             "shares": "ALL", "minAmount0": 0, "minAmount1": 0},
            {"op": "removeLiquidity", "actor": "LP",
             "shares": "ALL", "minAmount0": 0, "minAmount1": 0},
        ],
        [
            F("LP", 10**24, 10**24),
            F("bob", 5 * 10**23, 5 * 10**23),
            F("carol", 10**22, 0),
        ],
    ))

    return cases


# ---------------------------------------------------------------------
# random lifecycle cases
# ---------------------------------------------------------------------
def random_case(idx: int) -> dict:
    rng = random.Random(f"{SEED}:life:{idx}")
    name = f"lifecycle-{idx:02d}"

    base = 10 ** rng.randint(20, 26)
    skew = rng.choice([1, 1, 1, 3, 10, 100])  # skew the initial ratio
    a0 = base
    a1 = base * skew
    funds = [fund("alice", a0 + 10**24, a1 + 10**24)]
    seed_steps = [
        {"op": "addLiquidity", "actor": "alice", "amount0Desired": a0,
         "amount1Desired": a1, "minShares": 0}
    ]
    for actor in ("bob", "carol", "dave"):
        funds.append(fund(actor, base // 10 + 10**22, base // 10 + 10**22))

    pool = ReferencePool()
    for f in funds:
        pool.acct(f["actor"]).bal0 += f["amount0"]
        pool.acct(f["actor"]).bal1 += f["amount1"]
    # seed the model with the first mint so step generation can query state
    pool.add_liquidity("alice", a0, a1, 0, 10**18, 1_000_000)

    def actor_with_shares():
        cand = [a for a in ACTORS if pool.acct(a).shares > 1]
        return rng.choice(cand) if cand else None

    n_steps = rng.randint(8, 26)
    while len(seed_steps) < n_steps:
        roll = rng.random()
        actor = rng.choice(ACTORS)
        a = pool.acct(actor)
        if roll < 0.5 and pool.r0 > 10**6 and pool.r1 > 10**6:
            tin = rng.randint(0, 1)
            rin = pool.r0 if tin == 0 else pool.r1
            # keep <= 10% of reserve, and well above dust floor
            amount = rng.randint(10**12, max(10**12 + 1, rin // 10))
            bal_in = a.bal0 if tin == 0 else a.bal1
            rout_cur = pool.r1 if tin == 0 else pool.r0
            if bal_in < amount:
                continue
            if integer_amount_out(amount, rin, rout_cur) == 0:
                continue
            seed_steps.append(
                {"op": "swap", "actor": actor, "tokenIn": tin,
                 "amountIn": amount, "minAmountOut": 0}
            )
            # mirror so later generation decisions see post-swap reserves
            pool.swap(actor, tin, amount, 0, actor, 10**18, 1_000_000)
        elif roll < 0.78:
            # proportional add using both balances
            a = pool.acct(actor)
            cap = min(a.bal0, a.bal1)
            if cap < 10**6:
                continue
            want0 = rng.randint(10**6, max(10**6 + 1, cap))
            # choose want1 near current ratio
            ratio = pool.r1 / pool.r0
            want1 = int(want0 * ratio * rng.uniform(0.98, 1.02))
            if want1 > a.bal1 or want1 < 10**6:
                want1 = min(want1, a.bal1)
            if want1 < 10**6:
                continue
            x, y = integer_deposit_amounts(want0, want1, pool.r0, pool.r1)
            if x == 0 or y == 0:
                continue
            shares, _ = integer_mint_shares(x, y, pool.r0, pool.r1, pool.total_shares)
            if shares == 0:
                continue
            seed_steps.append(
                {"op": "addLiquidity", "actor": actor, "amount0Desired": want0,
                 "amount1Desired": want1, "minShares": 0}
            )
            # mirror into model so later generation sees updated state
            pool.add_liquidity(actor, want0, want1, 0, 10**18, 1_000_000)
        else:
            aw = actor_with_shares()
            if aw is None:
                continue
            held = pool.acct(aw).shares
            # never burn the lock / everything; burn 1..(held-1)
            burn = rng.randint(1, max(1, held - 1))
            x, y = integer_burn_amounts(burn, pool.r0, pool.r1, pool.total_shares)
            if x == 0 or y == 0:
                continue
            seed_steps.append(
                {"op": "removeLiquidity", "actor": aw, "shares": burn,
                 "minAmount0": 0, "minAmount1": 0}
            )
            pool.remove_liquidity(aw, burn, 0, 0, aw, 10**18, 1_000_000)

    # "ALL" support for proportional-round-trip handled before build:
    return build_case_from_seed(name, seed_steps, funds)


def build_case_from_seed(name, seed_steps, funds):
    # Resolve shares == "ALL" using the model while building.
    pool = ReferencePool()
    for f in funds:
        pool.acct(f["actor"]).bal0 += f["amount0"]
        pool.acct(f["actor"]).bal1 += f["amount1"]
    executed = []
    now = 1_000_000
    rng = random.Random(f"{SEED}:{name}")
    init_tot = (sum(f["amount0"] for f in funds), sum(f["amount1"] for f in funds))
    for st in seed_steps:
        now += rng.randint(1, 50)
        st = dict(st)
        st.setdefault("deadline", now + 3600)
        st.setdefault("to", st.get("actor"))
        if st.get("shares") == "ALL":
            st["shares"] = pool.acct(st["actor"]).shares
        executed.append(run_step(pool, st, now))
    assert_conservation({"name": name}, pool, init_tot)
    used = sorted(
        {f["actor"] for f in funds}
        | {s["step"].get("actor", "alice") for s in executed}
        | {s["step"].get("to", s["step"].get("actor")) for s in executed}
    )
    return {
        "name": name,
        "actors": used,
        "funds": funds,
        "steps": executed,
        "finalState": snap(pool),
    }


def main():
    os.makedirs(VEC_DIR, exist_ok=True)
    os.makedirs(EX_DIR, exist_ok=True)

    cases = targeted_cases()
    for i in range(N_CASES):
        cases.append(random_case(i))

    # explicit sweep of fee/rounding behaviour across scales for the manifest
    sweep = []
    for scale in (10**6, 10**12, 10**18, 10**24):
        for frac in (1, 2, 1000, 1001, 10**3, 10**6, 10**12):
            rin = rout = scale
            out_i = integer_amount_out(frac, rin, rout)
            real = continuous_amount_out(frac, rin, rout)
            record_gap(f"sweep-in={frac}-scale={scale}", real, out_i, bound="2",
                       reserves=(scale, scale, (frac * FEE_NUMERATOR) // FEE_DENOMINATOR))
            sweep.append({
                "reserveScale": scale,
                "amountIn": frac,
                "integerOut": out_i,
                "continuousOut": format(real, "f"),
            })

    doc = {
        "metadata": {
            "seed": hex(SEED),
            "feeNumerator": FEE_NUMERATOR,
            "feeDenominator": FEE_DENOMINATOR,
            "minimumLiquidity": MINIMUM_LIQUIDITY,
            "generatedBy": "reference/generate_vectors.py",
            "actorAddresses": ACTOR_ADDR,
        },
        "cases": cases,
    }
    blob = json.dumps(doc, indent=2, sort_keys=True)
    with open(os.path.join(VEC_DIR, "vectors.json"), "w") as fh:
        fh.write(blob)

    # Fee sanity (structural, size-independent):
    #   (a) whenever the integer output is positive, the fee is strict:
    #       inAfterFee = floor(in*997/1000) < in, and the realised output
    #       is strictly below the zero-fee x*y=k output;
    #   (b) whenever inAfterFee rounds to 0, the output is 0 as well — the
    #       chain rejects these, so no dust input ever trades fee-free.
    # Swept over reserve scales spanning 24 orders of magnitude and inputs
    # from 1 wei to the full reserve.
    fee_rows = []
    for scale in (10**6, 10**12, 10**18, 10**24, 10**30):
        rin = rout = scale
        for amount in [1, 2, 3, 10, 333, 334, 1000, 10_000, 10**12,
                       scale // 1000, scale // 100, scale // 10, scale]:
            in_after_fee = (amount * FEE_NUMERATOR) // FEE_DENOMINATOR
            out_i = integer_amount_out(amount, rin, rout)
            zero_fee_cont = Decimal(amount) * Decimal(rout) / (
                Decimal(rin) + Decimal(amount)
            )
            if out_i > 0:
                assert in_after_fee < amount, f"fee not strict: in={amount}"
                assert Decimal(out_i) < zero_fee_cont, f"free trade: in={amount}"
            else:
                assert in_after_fee == 0 or (
                    in_after_fee * rout) // (rin + in_after_fee) == 0, \
                    f"unexpected zero out: in={amount}"
            bps = effective_fee_bps(amount, rin, rout)
            fee_rows.append({"reserveScale": scale, "amountIn": amount,
                             "inAfterFee": in_after_fee, "integerOut": out_i,
                             "effectiveFeeBps": format(bps, "f")})

    manifest = {
        "caseCount": len(cases),
        "vectorSha256": hashlib.sha256(blob.encode()).hexdigest(),
        "maxFloorGap": format(max_gap, "f"),
        "feeBpsCheck": fee_rows,
        "precisionChecks": len(precision_rows),
    }
    with open(os.path.join(VEC_DIR, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)

    # human-readable sample sequence (first targeted case around a swap)
    sample = build_human_sample()
    with open(os.path.join(EX_DIR, "sample-sequence.json"), "w") as fh:
        json.dump(sample, fh, indent=2)

    with open(os.path.join(EX_DIR, "precision-report.txt"), "w") as fh:
        fh.write(format_report(manifest, sweep, fee_rows))

    print(f"wrote {len(cases)} cases -> test/vectors/vectors.json")
    print(f"sha256: {manifest['vectorSha256']}")
    print(f"precision checks: {manifest['precisionChecks']}, "
          f"max floor gap: {manifest['maxFloorGap']}")


def build_human_sample():
    """One lifecycle rendered with decimal amounts for README/anvil replay."""
    pool = ReferencePool()
    for who, b0, b1 in [("alice", 2000 * 10**18, 2000 * 10**18),
                        ("bob", 100 * 10**18, 100 * 10**18)]:
        a = pool.acct(who)
        a.bal0, a.bal1 = b0, b1
    log = []
    r = pool.add_liquidity("alice", 1000 * 10**18, 1000 * 10**18, 0, 10**18, 1_000_000)
    log.append({"op": "alice addLiquidity 1000/1000",
                "sharesMinted": r.shares, "locked": MINIMUM_LIQUIDITY,
                "state": snap(pool)})
    out = integer_amount_out(10 * 10**18, pool.r0, pool.r1)
    r = pool.swap("bob", 0, 10 * 10**18, 0, "bob", 10**18, 1_000_001)
    log.append({"op": "bob swap 10 TKN0 -> TKN1 (0.3% fee)",
                "amountOut": r.amount_out, "continuousOut":
                format(continuous_amount_out(10 * 10**18,
                                             1000 * 10**18, 1000 * 10**18), "f"),
                "state": snap(pool)})
    r = pool.add_liquidity("bob", 50 * 10**18, 50 * 10**18, 0, 10**18, 1_000_002)
    log.append({"op": "bob addLiquidity 50/50-ish proportional",
                "sharesMinted": r.shares, "state": snap(pool)})
    return {"description": "Human-readable lifecycle; integers are in wei "
                           "(18 decimals). Same math as vectors.json.",
            "feeBps": 30, "steps": log, "finalState": snap(pool)}


def format_report(manifest, sweep, fee_rows):
    lines = [
        "constant-product pool: integer vs 80-digit continuous model",
        "=" * 64,
        f"cases: {manifest['caseCount']}",
        f"arithmetic precision checks: {manifest['precisionChecks']}",
        f"max floor gap (real - floor): {manifest['maxFloorGap']}",
        "",
        "effective realised fee (bps) by input size, equal reserves:",
        f"{'reserve':>10} {'amountIn':>14} {'effFeeBps':>22}",
    ]
    for row in fee_rows:
        lines.append(f"{row['reserveScale']:>10} {row['amountIn']:>14} "
                     f"{row['effectiveFeeBps']:>22}")
    lines += ["", "scale sweep (reserves = scale each):",
              f"{'scale':>10} {'in':>10} {'intOut':>20}"]
    for row in sweep:
        lines.append(f"{row['reserveScale']:>10} {row['amountIn']:>10} "
                     f"{row['integerOut']:>20}")
    lines.append("")
    lines.append("Dust inputs produce integerOut = 0 and are REVERTED on chain")
    lines.append("(ZeroOutput); the fee can never be rounded away.")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
