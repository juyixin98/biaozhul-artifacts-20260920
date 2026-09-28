"""Process configuration, loaded from environment variables.

No third-party settings dependency: the surface is tiny, a frozen dataclass
with explicit env parsing keeps the dependency set small.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CHUNK_SIZE = 64
MIN_CHUNK_SIZE = 1
MAX_CHUNK_SIZE = 4096


@dataclass(frozen=True)
class Settings:
    db_path: str
    chunk_size: int
    log_level: str
    # When true, API payloads never echo raw document text snippets; logs are
    # always sanitized regardless of this flag.
    redact_snippets: bool

    @staticmethod
    def from_env(env: dict[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        raw_size = env.get("BRACKET_INDEX_CHUNK_SIZE", str(DEFAULT_CHUNK_SIZE))
        try:
            chunk_size = int(raw_size)
        except ValueError as exc:
            raise ValueError(
                f"BRACKET_INDEX_CHUNK_SIZE must be an integer, got {raw_size!r}"
            ) from exc
        if not (MIN_CHUNK_SIZE <= chunk_size <= MAX_CHUNK_SIZE):
            raise ValueError(
                "BRACKET_INDEX_CHUNK_SIZE must be in "
                f"[{MIN_CHUNK_SIZE}, {MAX_CHUNK_SIZE}], got {chunk_size}"
            )
        db_path = env.get("BRACKET_INDEX_DB", str(Path.cwd() / "bracket_index.db"))
        return Settings(
            db_path=db_path,
            chunk_size=chunk_size,
            log_level=env.get("BRACKET_INDEX_LOG_LEVEL", "INFO").upper(),
            redact_snippets=env.get("BRACKET_INDEX_REDACT_SNIPPETS", "1")
            not in ("0", "false", "False", ""),
        )
