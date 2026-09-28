"""Configuration layer.

All settings have safe local defaults and can be overridden through environment
variables (prefix ``SPM_``), so the service runs fully offline without any
production account or external dependency.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _as_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _as_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw


BASE_DIR = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    """Immutable runtime settings (see :mod:`coding-style` immutability rule)."""

    db_path: str
    log_dir: str
    max_pattern_length: int
    max_gap_position: int
    # Timestamps are arbitrary numeric units; this only bounds user input.
    max_gap_time: float
    max_sequence_events: int
    max_corpus_sequences: int

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            db_path=_as_str("SPM_DB_PATH", str(BASE_DIR / "data" / "spm.db")),
            log_dir=_as_str("SPM_LOG_DIR", str(BASE_DIR / "logs")),
            max_pattern_length=_as_int("SPM_MAX_PATTERN_LENGTH", 64),
            max_gap_position=_as_int("SPM_MAX_GAP_POSITION", 10_000),
            max_gap_time=float(_as_int("SPM_MAX_GAP_TIME", 1_000_000)),
            max_sequence_events=_as_int("SPM_MAX_SEQUENCE_EVENTS", 10_000),
            max_corpus_sequences=_as_int("SPM_MAX_CORPUS_SEQUENCES", 10_000),
        )


settings = Settings.from_env()
