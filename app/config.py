"""Service configuration.

Everything tunable lives here; request handlers never read environment
variables directly. Error budgets and the deployed model location are
configuration, not per-request state.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    model_id: str = "tiny-matmul-demo"
    model_version: str = "2026-09-28-v1"
    fixture_dir: str = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures"
    )
    max_batch: int = 64
    max_features: int = 256
    log_redact_payloads: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            model_id=os.getenv("QINFER_MODEL_ID", cls.model_id),
            model_version=os.getenv("QINFER_MODEL_VERSION", cls.model_version),
            fixture_dir=os.getenv("QINFER_FIXTURE_DIR", cls.fixture_dir),
            max_batch=int(os.getenv("QINFER_MAX_BATCH", str(cls.max_batch))),
            max_features=int(os.getenv("QINFER_MAX_FEATURES", str(cls.max_features))),
        )
