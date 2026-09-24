"""Reference implementation and on-chain cross-check helpers.

The contract answers queries with an upper-bound *binary* search. Here we
implement the same semantics with an explicit *linear* scan plus a local
model of block/value history, so the tests can cross-validate every on-chain
answer against an independent algorithm.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReferenceCheckpoint:
    block_number: int
    value: int


class ReferenceCheckpoints:
    """Linear-scan model with identical merge/lookup semantics."""

    def __init__(self) -> None:
        # One checkpoint per touched block, strictly increasing by block.
        self._checkpoints: list[ReferenceCheckpoint] = []

    def set_value(self, block_number: int, value: int) -> None:
        """Merge same-block updates (last write wins); append on a new block."""
        if self._checkpoints and self._checkpoints[-1].block_number == block_number:
            self._checkpoints[-1] = ReferenceCheckpoint(block_number, value)
        else:
            self._checkpoints.append(ReferenceCheckpoint(block_number, value))

    def linear_lookup(self, target_block: int, current_block: int):
        """Last checkpoint at or before target_block.

        Returns None when no such checkpoint exists. Raises ValueError when
        target_block > current_block (mirrors the contract's FutureBlock).
        """
        if target_block > current_block:
            raise ValueError(
                f"future block: requested {target_block}, current {current_block}"
            )
        answer: ReferenceCheckpoint | None = None
        for cp in self._checkpoints:  # explicit linear scan, O(n)
            if cp.block_number <= target_block:
                answer = cp
            else:
                break
        return answer

    @property
    def checkpoints(self) -> tuple[ReferenceCheckpoint, ...]:
        return tuple(self._checkpoints)
