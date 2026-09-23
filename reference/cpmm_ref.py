#!/usr/bin/env python3
"""Independent reference model for the constant-product pool.

Two independent descriptions of the same economics live here:

* ``integer_*`` functions -- exact EVM semantics: every division is a floor
  (``//``), uint256 arithmetic, matching the deployed Solidity one-for-one.
* ``continuous_*`` functions -- fee-inclusive x*y=k formulas evaluated in
  arbitrary-precision Decimal (>=80 digits). They have NO rounding and serve
  as the ground truth the integer results must sit next to, with the floor
  gap bounded.

A stateful ``ReferencePool`` replays whole operation sequences (the same
JSON vectors the Solidity tests replay) and emits expected post-state.

The model deliberately contains nothing copied from the Solidity source: it
is written from the specification in README (0.30% input fee, 1000-share
lock, floor rounding in favour of the pool) so that agreement is genuine
cross-implementation agreement, not tautology.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal, getcontext
from enum import Enum
from typing import Optional

# 80 decimal digits is far beyond anything uint256 reserves can stress
# (log10(2**256) ~= 77 digits), so the continuous model has full precision
# headroom over every on-chain input.
getcontext().prec = 80

FEE_NUMERATOR = 997
FEE_DENOMINATOR = 1000
MINIMUM_LIQUIDITY = 1000
UINT112_MAX = (1 << 112) - 1


# ----------------------------------------------------------------------
# Errors — name strings match the JSON vector "revert" field and the
# Solidity test maps them to the contract's custom-error selectors.
# ----------------------------------------------------------------------
class RefError(str, Enum):
    SAME_TOKEN = "SameToken"
    ZERO_ADDRESS = "ZeroAddress"
    EXPIRED = "Expired"
    ZERO_AMOUNT = "ZeroAmount"
    ZERO_OUTPUT = "ZeroOutput"
    ZERO_RECIPIENT = "ZeroRecipient"
    NO_LIQUIDITY_MINTED = "NoLiquidityMinted"
    INSUFFICIENT_SHARES = "InsufficientShares"
    SLIPPAGE_SHARES = "SlippageShares"
    SLIPPAGE_TOKEN = "SlippageToken"
    INVALID_TOKEN_IN = "InvalidTokenIn"
    MIN_OUTPUT_NOT_MET = "MinOutputNotMet"
    TRANSFER_FAILED = "TransferFailed"


class RefRevert(Exception):
    """Raised for any operation the contract must atomically reject."""

    def __init__(self, code: RefError):
        super().__init__(code.value)
        self.code = code.value


# ----------------------------------------------------------------------
# Integer math (EVM semantics). The floor sqrt uses Python's exact
# math.isqrt -- an independent implementation from the Solidity Babylonian
# loop; agreement on every vector is cross-implementation evidence.
# ----------------------------------------------------------------------
def integer_amount_out(amount_in: int, reserve_in: int, reserve_out: int) -> int:
    """out = floor( floor(in * 997 / 1000) * rOut / (rIn + that) )."""
    in_after_fee = (amount_in * FEE_NUMERATOR) // FEE_DENOMINATOR
    if in_after_fee == 0:
        return 0
    return (in_after_fee * reserve_out) // (reserve_in + in_after_fee)


def integer_mint_shares(
    amount0: int, amount1: int, r0: int, r1: int, total_shares: int
) -> tuple[int, bool]:
    if total_shares == 0:
        raw = math.isqrt(amount0 * amount1)
        shares = raw - MINIMUM_LIQUIDITY if raw > MINIMUM_LIQUIDITY else 0
        return shares, True
    s0 = (amount0 * total_shares) // r0
    s1 = (amount1 * total_shares) // r1
    return min(s0, s1), False


def integer_burn_amounts(
    shares: int, r0: int, r1: int, total_shares: int
) -> tuple[int, int]:
    return (shares * r0) // total_shares, (shares * r1) // total_shares


def integer_deposit_amounts(
    a0_desired: int, a1_desired: int, r0: int, r1: int
) -> tuple[int, int]:
    """Optimal proportional amounts actually pulled (matches the pool)."""
    opt1 = (a0_desired * r1) // r0
    if opt1 <= a1_desired:
        return a0_desired, opt1
    opt0 = (a1_desired * r0) // r1
    return opt0, a1_desired


# ----------------------------------------------------------------------
# Continuous high-precision model (no integer rounding anywhere)
# ----------------------------------------------------------------------
def continuous_amount_out(
    amount_in: int, reserve_in: int, reserve_out: int
) -> Decimal:
    """Exact x*y=k output with proportional input fee, in Decimal."""
    a = Decimal(amount_in) * Decimal(FEE_NUMERATOR) / Decimal(FEE_DENOMINATOR)
    ri = Decimal(reserve_in)
    ro = Decimal(reserve_out)
    return a * ro / (ri + a)


def continuous_mint_shares(
    amount0: int, amount1: int, r0: int, r1: int, total_shares: int
) -> Decimal:
    if total_shares == 0:
        return (Decimal(amount0) * Decimal(amount1)).sqrt() - Decimal(
            MINIMUM_LIQUIDITY
        )
    a0, a1 = Decimal(amount0), Decimal(amount1)
    return min(a0 * Decimal(total_shares) / Decimal(r0),
               a1 * Decimal(total_shares) / Decimal(r1))


def continuous_burn_amounts(
    shares: int, r0: int, r1: int, total_shares: int
) -> tuple[Decimal, Decimal]:
    s = Decimal(shares)
    ts = Decimal(total_shares)
    return s * Decimal(r0) / ts, s * Decimal(r1) / ts


def floor_gap(real: Decimal, floored: int) -> Decimal:
    """0 <= real - floor(real) < 1, asserted per arithmetic result."""
    return real - Decimal(floored)


# ----------------------------------------------------------------------
# Fee check: the floor-fee is economically real at every scale.
# ----------------------------------------------------------------------
def effective_fee_bps(amount_in: int, reserve_in: int, reserve_out: int) -> Decimal:
    """Actual realised fee in bps once rounding is accounted for.

    Compares the realised output to the *zero-fee* x*y=k output. For dust
    inputs the integer output rounds to 0 -> effective fee is 100% (the swap
    is rejected on chain), never 0.
    """
    if amount_in == 0 or reserve_in == 0 or reserve_out == 0:
        return Decimal(0)
    zero_fee = Decimal(amount_in) * Decimal(reserve_out) / (
        Decimal(reserve_in) + Decimal(amount_in)
    )
    out = Decimal(integer_amount_out(amount_in, reserve_in, reserve_out))
    if zero_fee == 0:
        return Decimal(0)
    return (1 - out / zero_fee) * Decimal(10_000)


# ----------------------------------------------------------------------
# Stateful pool used to replay sequences
# ----------------------------------------------------------------------
@dataclass
class Account:
    bal0: int = 0
    bal1: int = 0
    shares: int = 0


@dataclass
class StepResult:
    name: str
    reverted: Optional[str]
    # values populated for successful steps
    amount0: Optional[int] = None
    amount1: Optional[int] = None
    shares: Optional[int] = None
    amount_out: Optional[int] = None


@dataclass
class ReferencePool:
    """Deterministic integer simulation of one pool's full lifecycle."""

    r0: int = 0
    r1: int = 0
    total_shares: int = 0
    accounts: dict[str, Account] = field(default_factory=dict)

    # The minimum-liquidity lock is modelled as an account "LOCK" whose
    # shares can never move, exactly like address(0) on chain.
    LOCK = "LOCK"

    def __post_init__(self):
        self.accounts[self.LOCK] = Account()

    def acct(self, who: str) -> Account:
        if who not in self.accounts:
            self.accounts[who] = Account()
        return self.accounts[who]

    # ---- helpers ----------------------------------------------------
    def _deadline_ok(self, deadline: int, now: int) -> None:
        if now > deadline:
            raise RefRevert(RefError.EXPIRED)

    def _mint(self, who: str, shares: int) -> None:
        self.total_shares += shares
        self.acct(who).shares += shares

    def _burn(self, who: str, shares: int) -> None:
        a = self.acct(who)
        if a.shares < shares:
            raise RefRevert(RefError.INSUFFICIENT_SHARES)
        a.shares -= shares
        self.total_shares -= shares

    # ---- operations -------------------------------------------------
    def add_liquidity(self, who, a0d, a1d, min_shares, deadline, now):
        if a0d == 0 or a1d == 0:
            raise RefRevert(RefError.ZERO_AMOUNT)
        self._deadline_ok(deadline, now)
        a = self.acct(who)

        # Phase 1: compute + validate ONLY (mirrors EVM atomicity: nothing
        # may mutate before every revert condition has been checked).
        if self.total_shares == 0:
            amount0, amount1 = a0d, a1d
            raw = math.isqrt(amount0 * amount1)
            if raw <= MINIMUM_LIQUIDITY:
                raise RefRevert(RefError.NO_LIQUIDITY_MINTED)
            shares = raw - MINIMUM_LIQUIDITY
            first = True
        else:
            amount0, amount1 = integer_deposit_amounts(a0d, a1d, self.r0, self.r1)
            if amount0 == 0 or amount1 == 0:
                raise RefRevert(RefError.ZERO_AMOUNT)
            shares, first = integer_mint_shares(
                amount0, amount1, self.r0, self.r1, self.total_shares
            )
            if shares == 0:
                raise RefRevert(RefError.INSUFFICIENT_SHARES)

        if shares < min_shares:
            raise RefRevert(RefError.SLIPPAGE_SHARES)
        if a.bal0 < amount0 or a.bal1 < amount1:
            raise RefRevert(RefError.TRANSFER_FAILED)

        # Phase 2: commit.
        if first:
            a.bal0 -= amount0
            a.bal1 -= amount1
            self.r0 += amount0
            self.r1 += amount1
            self._mint(self.LOCK, MINIMUM_LIQUIDITY)
            self._mint(who, shares)
        else:
            a.bal0 -= amount0
            a.bal1 -= amount1
            self.r0 += amount0
            self.r1 += amount1
            self._mint(who, shares)

        return StepResult("addLiquidity", None, amount0, amount1, shares)

    def remove_liquidity(self, who, shares, min0, min1, to, deadline, now):
        if shares == 0:
            raise RefRevert(RefError.ZERO_AMOUNT)
        self._deadline_ok(deadline, now)
        a0, a1 = integer_burn_amounts(shares, self.r0, self.r1, self.total_shares)
        if a0 == 0 or a1 == 0:
            raise RefRevert(RefError.ZERO_OUTPUT)
        if a0 < min0 or a1 < min1:
            raise RefRevert(RefError.SLIPPAGE_TOKEN)
        self._burn(who, shares)  # raises InsufficientShares if needed
        dest = self.acct(to)
        dest.bal0 += a0
        dest.bal1 += a1
        self.r0 -= a0
        self.r1 -= a1
        return StepResult("removeLiquidity", None, a0, a1, shares)

    def swap(self, who, token_in, amount_in, min_out, to, deadline, now):
        if amount_in == 0:
            raise RefRevert(RefError.ZERO_AMOUNT)
        self._deadline_ok(deadline, now)
        if token_in not in (0, 1):
            raise RefRevert(RefError.INVALID_TOKEN_IN)
        rin, rout = (self.r0, self.r1) if token_in == 0 else (self.r1, self.r0)
        out = integer_amount_out(amount_in, rin, rout)
        if out == 0:
            raise RefRevert(RefError.ZERO_OUTPUT)
        if out < min_out:
            raise RefRevert(RefError.MIN_OUTPUT_NOT_MET)
        a = self.acct(who)
        in_bal = a.bal0 if token_in == 0 else a.bal1
        if in_bal < amount_in:
            raise RefRevert(RefError.TRANSFER_FAILED)
        if token_in == 0:
            a.bal0 -= amount_in
            self.acct(to).bal1 += out
            self.r0 += amount_in
            self.r1 -= out
        else:
            a.bal1 -= amount_in
            self.acct(to).bal0 += out
            self.r1 += amount_in
            self.r0 -= out
        return StepResult("swap", None, amount_in, None, None, out)

    # ---- state snapshot --------------------------------------------
    def snapshot(self) -> dict:
        # Keys are in alphabetical order on purpose: Solidity's
        # vm.parseJson abi-encodes object members in that order, so the
        # replaying test declares its structs in the same order. Only
        # `accountList` (ordered array) is emitted; a second object form
        # would be encoded as an extra tuple member and misalign decoding.
        return {
            "accountList": [
                {"bal0": v.bal0, "bal1": v.bal1, "key": k, "shares": v.shares}
                for k, v in sorted(self.accounts.items())
            ],
            "lockedShares": self.accounts[self.LOCK].shares,
            "reserve0": self.r0,
            "reserve1": self.r1,
            "totalShares": self.total_shares,
        }

    def k(self) -> int:
        return self.r0 * self.r1
