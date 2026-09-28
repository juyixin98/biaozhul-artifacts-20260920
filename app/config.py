"""Configuration layer: all tunables come from here, overridable via env vars."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    db_path: str
    max_pattern_len: int
    max_embeddings_per_sequence: int
    log_level: str


def load_settings() -> Settings:
    return Settings(
        db_path=os.environ.get("FSPM_DB_PATH", "fspm.db"),
        max_pattern_len=int(os.environ.get("FSPM_MAX_PATTERN_LEN", "8")),
        max_embeddings_per_sequence=int(
            os.environ.get("FSPM_MAX_EMBEDDINGS_PER_SEQUENCE", "64")
        ),
        log_level=os.environ.get("FSPM_LOG_LEVEL", "INFO"),
    )
