"""High-precision independent reference model for the CPMM pool.

Two parallel models are provided:

* ``evm_*`` functions — exact EVM integer semantics (floor division, uint256),
  an independent re-implementation of the Solidity math in ``src/CPMMMath.sol``
  and ``src/CPMMPair.sol``.
* ``real_*`` functions — real arithmetic using :class:`decimal.Decimal` at
  100 significant digits, used to bound integer rounding losses.

The model is deliberately dependency-free (stdlib only).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, getcontext
from math import isqrt

getcontext().prec = 100

MINIMUM_LIQUIDITY = 1_000
FEE_NUM = 997
FEE_DEN = 1_000


# ---------------------------------------------------------------------------
# EVM integer semantics (must mirror the Solidity exactly)
# ---------------------------------------------------------------------------

def evm_get_amount_out(amount_in: int, reserve_in: int, reserve_out: int) -> int:
    assert amount_in > 0 and reserve_in > 0 and reserve_out > 0
    in_with_fee = amount_in * FEE_NUM
    return (in_with_fee * reserve_out) // (reserve_in * FEE_DEN + in_with_fee)


def evm_minted_liquidity(amount_a: int, amount_b: int, reserve_a: int,
                         reserve_b: int, total_supply: int) -> int:
    la = (amount_a * total_supply) // reserve_a
    lb = (amount_b * total_supply) // reserve_b
    return min(la, lb)


def evm_burned_amounts(liquidity: int, total_supply: int, reserve_a: int,
                       reserve_b: int) -> tuple[int, int]:
    return ((liquidity * reserve_a) // total_supply,
            (liquidity * reserve_b) // total_supply)


# ---------------------------------------------------------------------------
# Real arithmetic (100-digit Decimal), the "ground truth" ideal curve
# ---------------------------------------------------------------------------

def real_get_amount_out(amount_in: int, reserve_in: int, reserve_out: int) -> Decimal:
    ain = Decimal(amount_in) * Decimal(FEE_NUM) / Decimal(FEE_DEN)
    return ain * Decimal(reserve_out) / (Decimal(reserve_in) + ain)


def real_first_liquidity(amount0: int, amount1: int) -> Decimal:
    return Decimal(amount0) * Decimal(amount1)


def real_minted_liquidity(amount_a: int, amount_b: int, reserve_a: int,
                          reserve_b: int, total_supply: int) -> Decimal:
    la = Decimal(amount_a * total_supply) / Decimal(reserve_a)
    lb = Decimal(amount_b * total_supply) / Decimal(reserve_b)
    return min(la, lb)


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

class ModelRevert(Exception):
    """A step reverts; on-chain the whole transaction rolls back."""


@dataclass
class User:
    token0: int = 0
    token1: int = 0
    lp: int = 0


@dataclass
class PoolState:
    reserve0: int = 0
    reserve1: int = 0
    balance0: int = 0
    balance1: int = 0
    total_supply: int = 0
    locked_lp: int = 0  # shares held by address(0)


@dataclass
class CPMMPoolModel:
    """Sequential model of one CPMMPair + user balances.

    Every successful op leaves ``balance == reserve`` (no donations), which is
    exactly what the on-chain tests assert as well.
    """

    users: dict[str, User] = field(default_factory=dict)
    pool: PoolState = field(default_factory=PoolState)
    log: list[dict] = field(default_factory=list)

    # -- helpers -----------------------------------------------------------

    def user(self, name: str) -> User:
        if name not in self.users:
            self.users[name] = User()
        return self.users[name]

    def fund(self, name: str, amount0: int, amount1: int) -> None:
        u = self.user(name)
        u.token0 += amount0
        u.token1 += amount1

    def snapshot(self) -> dict:
        return {
            "reserve0": str(self.pool.reserve0),
            "reserve1": str(self.pool.reserve1),
            "bal0": str(self.pool.balance0),
            "bal1": str(self.pool.balance1),
            "totalSupply": str(self.pool.total_supply),
            "lockLp": str(self.pool.locked_lp),
            "users": {
                n: {"token0": str(u.token0), "token1": str(u.token1),
                    "lp": str(u.lp)}
                for n, u in sorted(self.users.items())
            },
        }

    def _check_consistency(self) -> None:
        p = self.pool
        # Reserves always equal real balances in this no-donation model.
        assert p.balance0 == p.reserve0
        assert p.balance1 == p.reserve1
        # Locked shares are immutable after bootstrap.
        if p.total_supply:
            assert p.locked_lp == MINIMUM_LIQUIDITY
        # Sum of LP shares.
        circulating = sum(u.lp for u in self.users.values())
        assert circulating + p.locked_lp == p.total_supply

    # -- operations --------------------------------------------------------

    def deposit(self, name: str, amount0: int, amount1: int) -> dict:
        u = self.user(name)
        p = self.pool
        if amount0 <= 0 or amount1 <= 0:
            raise ModelRevert("CPMM: INSUFFICIENT_INPUT")
        if u.token0 < amount0 or u.token1 < amount1:
            raise ModelRevert("user funding")

        real_liq: Decimal
        if p.total_supply == 0:
            root = isqrt(amount0 * amount1)
            liquidity = root - MINIMUM_LIQUIDITY
            if liquidity <= 0:
                raise ModelRevert("CPMM: INSUFFICIENT_FIRST_LIQUIDITY")
            p.locked_lp = MINIMUM_LIQUIDITY
            total_after = MINIMUM_LIQUIDITY + liquidity
            p.total_supply = total_after
            real_liq = real_first_liquidity(amount0, amount1).sqrt() - MINIMUM_LIQUIDITY
        else:
            liquidity = evm_minted_liquidity(
                amount0, amount1, p.reserve0, p.reserve1, p.total_supply)
            if liquidity == 0:
                raise ModelRevert("CPMM: INSUFFICIENT_FIRST_LIQUIDITY")
            p.total_supply += liquidity
            real_liq = real_minted_liquidity(
                amount0, amount1, p.reserve0, p.reserve1, p.total_supply - liquidity)

        u.token0 -= amount0
        u.token1 -= amount1
        u.lp += liquidity
        p.balance0 += amount0
        p.balance1 += amount1
        p.reserve0 = p.balance0
        p.reserve1 = p.balance1

        self._check_consistency()
        return {
            "liquidity": str(liquidity),
            "realLiquidity": str(real_liq),
        }

    def swap(self, name: str, token_in: str, amount_in: int) -> dict:
        assert token_in in ("token0", "token1")
        u = self.user(name)
        p = self.pool
        if amount_in <= 0:
            raise ModelRevert("CPMM: INSUFFICIENT_INPUT")
        in_bal = u.token0 if token_in == "token0" else u.token1
        if in_bal < amount_in:
            raise ModelRevert("user funding")

        rin = p.reserve0 if token_in == "token0" else p.reserve1
        rout = p.reserve1 if token_in == "token0" else p.reserve0
        amount_out = evm_get_amount_out(amount_in, rin, rout)
        if amount_out == 0:
            raise ModelRevert("CPMM: INSUFFICIENT_OUTPUT_AMOUNT (rounds to 0)")

        # k-invariant verification (independent recomputation)
        if token_in == "token0":
            b0, b1 = p.balance0 + amount_in, p.balance1 - amount_out
            ain0, ain1 = amount_in, 0
        else:
            b0, b1 = p.balance0 - amount_out, p.balance1 + amount_in
            ain0, ain1 = 0, amount_in
        adj0 = b0 * 1000 - ain0 * 3
        adj1 = b1 * 1000 - ain1 * 3
        if adj0 * adj1 < p.reserve0 * p.reserve1 * 1_000_000:
            raise ModelRevert("CPMM: K_INVARIANT")

        if token_in == "token0":
            u.token0 -= amount_in
            u.token1 += amount_out
        else:
            u.token1 -= amount_in
            u.token0 += amount_out
        p.balance0, p.balance1 = b0, b1
        p.reserve0, p.reserve1 = b0, b1

        real_out = real_get_amount_out(amount_in, rin, rout)
        # Floor must never exceed the real curve output.
        assert Decimal(amount_out) <= real_out
        # Floor loss is strictly less than 1 wei by construction.
        assert real_out - Decimal(amount_out) < Decimal(1)

        self._check_consistency()
        return {"amountOut": str(amount_out), "realAmountOut": str(real_out)}

    def burn(self, name: str, liquidity: int) -> dict:
        u = self.user(name)
        p = self.pool
        if liquidity <= 0 or liquidity > u.lp:
            raise ModelRevert("CPMM: INSUFFICIENT_LIQUIDITY")
        a0, a1 = evm_burned_amounts(liquidity, p.total_supply,
                                    p.reserve0, p.reserve1)
        if a0 == 0 or a1 == 0:
            raise ModelRevert("CPMM: INSUFFICIENT_FIRST_LIQUIDITY")

        u.lp -= liquidity
        p.total_supply -= liquidity
        u.token0 += a0
        u.token1 += a1
        p.balance0 -= a0
        p.balance1 -= a1
        p.reserve0, p.reserve1 = p.balance0, p.balance1

        self._check_consistency()
        return {"amount0": str(a0), "amount1": str(a1)}

    def donate_and_skim(self, donor: str, amount0: int, amount1: int,
                        recipient: str) -> dict:
        """Donate tokens without minting (reserves stay stale), then skim."""
        d = self.user(donor)
        r = self.user(recipient)
        d.token0 -= amount0
        d.token1 -= amount1
        # During donation balances exceed reserves.
        self.pool.balance0 += amount0
        self.pool.balance1 += amount1
        out0 = self.pool.balance0 - self.pool.reserve0
        out1 = self.pool.balance1 - self.pool.reserve1
        self.pool.balance0 -= out0
        self.pool.balance1 -= out1
        r.token0 += out0
        r.token1 += out1
        self._check_consistency()
        return {"amount0": str(out0), "amount1": str(out1)}
