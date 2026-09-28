"""Digit budget accounting for exact elimination.

Integer coefficient growth during fraction-free (Bareiss) elimination is
bounded in *size*, not just count. This module tracks:

* the maximum number of **decimal digits** any integer coefficient (numerator
  or denominator of a ``Fraction``) is allowed to reach;
* a soft safety cap on elimination steps;
* per-stage observed digit maxima, so an exhausted budget can be reported with
  a diagnosable intermediate snapshot rather than a silent timeout or float
  fallback.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Iterable

DEFAULT_DIGIT_BUDGET = 4096
# A generous structural cap: Bareiss on an m x n matrix does at most
# min(m,n) pivots; allow callers to additionally bound pathological repeats.
DEFAULT_STEP_BUDGET = 1_000_000

_BITS_PER_DECIMAL_DIGIT = math.log2(10.0)


def decimal_digits(value: int) -> int:
    """Number of decimal digits in ``abs(value)`` (0 maps to 1)."""
    if value == 0:
        return 1
    magnitude = abs(value)
    # Tight estimate from bit length, then exact correction with integer
    # powers of 10 (avoids materialising a decimal string for big integers).
    digits = int((magnitude.bit_length() - 1) * math.log10(2)) + 1
    while 10**digits <= magnitude:
        digits += 1
    while 10 ** (digits - 1) > magnitude:
        digits -= 1
    return digits


def fraction_digits(value: Fraction) -> int:
    return max(decimal_digits(value.numerator), decimal_digits(value.denominator))


@dataclass
class BudgetSnapshot:
    digit_limit: int
    step_limit: int
    steps_used: int
    max_digits_seen: int
    stage: str
    last_pivot_index: int

    def to_dict(self) -> dict:
        return {
            "digit_limit": self.digit_limit,
            "step_limit": self.step_limit,
            "steps_used": self.steps_used,
            "max_digits_seen": self.max_digits_seen,
            "stage": self.stage,
            "last_pivot_index": self.last_pivot_index,
        }


@dataclass
class DigitBudget:
    """Mutable budget ledger shared with the elimination kernel."""

    digit_limit: int = DEFAULT_DIGIT_BUDGET
    step_limit: int = DEFAULT_STEP_BUDGET
    steps_used: int = 0
    max_digits_seen: int = 1
    stage: str = "init"
    last_pivot_index: int = -1
    #: ordered record of (pivot_index, observed max digits) for replay
    trace: list[tuple[int, int]] = field(default_factory=list)

    def enter_stage(self, stage: str) -> None:
        self.stage = stage

    def note_pivot(self, pivot_index: int) -> None:
        self.last_pivot_index = pivot_index
        self.steps_used += 1
        if self.steps_used > self.step_limit:
            self._raise("elimination step budget exceeded", "STEP_BUDGET_EXCEEDED")

    def observe(self, values: Iterable[Fraction]) -> None:
        """Check every fraction in ``values`` against the digit budget."""
        observed = self.max_digits_seen
        for value in values:
            digits = fraction_digits(value)
            if digits > observed:
                observed = digits
        self.max_digits_seen = observed
        if observed > self.digit_limit:
            self._raise(
                f"integer coefficient grew to {observed} decimal digits, "
                f"exceeding budget {self.digit_limit}",
                "DIGIT_BUDGET_EXCEEDED",
            )

    def snapshot(self) -> BudgetSnapshot:
        return BudgetSnapshot(
            digit_limit=self.digit_limit,
            step_limit=self.step_limit,
            steps_used=self.steps_used,
            max_digits_seen=self.max_digits_seen,
            stage=self.stage,
            last_pivot_index=self.last_pivot_index,
        )

    def record_pivot_trace(self) -> None:
        self.trace.append((self.last_pivot_index, self.max_digits_seen))

    def _raise(self, message: str, code: str):
        # Local import: errors must not depend on budget.
        from .errors import ResourceExhaustedError

        raise ResourceExhaustedError(
            message,
            code=code,
            details={
                "digit_limit": self.digit_limit,
                "step_limit": self.step_limit,
                "steps_used": self.steps_used,
                "max_digits_seen": self.max_digits_seen,
            },
        )
