"""Application configuration.

All configuration is environment-driven with local defaults - no production
accounts or external services.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    models_dir: str
    host: str
    port: int
    log_level: str
    enforce_calibration_range: bool
    max_batch_size: int
    max_features: int

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            models_dir=os.environ.get("QENGINE_MODELS_DIR", "models"),
            host=os.environ.get("QENGINE_HOST", "127.0.0.1"),
            port=int(os.environ.get("QENGINE_PORT", "8000")),
            log_level=os.environ.get("QENGINE_LOG_LEVEL", "INFO").upper(),
            enforce_calibration_range=(
                os.environ.get("QENGINE_ENFORCE_CALIBRATION_RANGE", "1") != "0"
            ),
            max_batch_size=int(os.environ.get("QENGINE_MAX_BATCH", "64")),
            max_features=int(os.environ.get("QENGINE_MAX_FEATURES", "4096")),
        )
