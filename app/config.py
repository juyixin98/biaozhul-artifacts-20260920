"""Runtime configuration, read from environment variables (12-factor)."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class Settings:
    database_url: str = os.getenv(
        "DATABASE_URL",
        "postgresql://epe:epe_dev_pw@localhost:5432/epe",
    )
    # Judgment / ruleset version stamped on every piece of evidence and penalty.
    # Deterministic protocol constant for v1; bumped only when rules change.
    judgment_version: str = os.getenv("JUDGMENT_VERSION", "rules-1.0.0")
    # BIP-128-like: slash fraction is slash_rate_ppm / 1_000_000 of frozen stake.
    slash_rate_ppm: int = int(os.getenv("SLASH_RATE_PPM", "10000"))  # 1%
    # Epoch length in rounds (rounds are 0-based and contiguous).
    rounds_per_epoch: int = int(os.getenv("ROUNDS_PER_EPOCH", "10"))
    # Test-only fault injection: "crash_after_evidence" makes the penalty step
    # raise immediately after evidence is committed, simulating a process crash
    # between the two transactions. Never enable in production.
    crash_after_evidence: bool = os.getenv("CRASH_AFTER_EVIDENCE", "0") == "1"


settings = Settings()
