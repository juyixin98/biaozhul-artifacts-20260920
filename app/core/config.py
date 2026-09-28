"""Configuration for certification runs.

All tunable limits live here so that "resource exhausted" is always the result
of an explicit, visible budget rather than an arbitrary internal cap.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _env_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    return raw if raw is not None and raw.strip() != "" else default


@dataclass(frozen=True)
class CertConfig:
    """Immutable run configuration.

    Attributes:
        precision_dps: mpmath working precision in decimal digits.
        target_width: stop shrinking a certified enclosure below this width.
        max_depth: maximum bisection depth along any branch.
        max_evals: maximum number of interval (range) evaluations per run.
        max_newton_iters: Newton iterations used to tighten one enclosure.
        max_expression_len: rejected-before-parsing expression length cap.
        display_digits: significant digits in outward-rounded decimal output.
    """

    precision_dps: int = 50
    target_width: str = "1e-20"
    max_depth: int = 90
    max_evals: int = 200_000
    max_newton_iters: int = 200
    max_expression_len: int = 2_000
    display_digits: int = 40

    @staticmethod
    def from_env() -> "CertConfig":
        return CertConfig(
            precision_dps=_env_int("RC_PRECISION_DPS", 50),
            target_width=_env_str("RC_TARGET_WIDTH", "1e-20"),
            max_depth=_env_int("RC_MAX_DEPTH", 90),
            max_evals=_env_int("RC_MAX_EVALS", 200_000),
            max_newton_iters=_env_int("RC_MAX_NEWTON_ITERS", 200),
            max_expression_len=_env_int("RC_MAX_EXPRESSION_LEN", 2_000),
            display_digits=_env_int("RC_DISPLAY_DIGITS", 40),
        )

    def validated(self) -> "CertConfig":
        """Fail fast on contradictory limits (a state conflict in config)."""
        if self.precision_dps < 10:
            raise ValueError("precision_dps must be >= 10")
        if self.max_depth < 1 or self.max_evals < 10:
            raise ValueError("max_depth/max_evals are too small")
        if self.max_newton_iters < 1:
            raise ValueError("max_newton_iters must be >= 1")
        if self.display_digits >= self.precision_dps:
            raise ValueError("display_digits must be smaller than precision_dps")
        return self
