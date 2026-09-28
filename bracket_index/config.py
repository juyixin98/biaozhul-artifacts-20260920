"""Runtime configuration, sourced from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass

ENV_DB_PATH = "BRACKET_INDEX_DB_PATH"
ENV_CHUNK_SIZE = "BRACKET_INDEX_CHUNK_SIZE"
ENV_LOG_LEVEL = "BRACKET_INDEX_LOG_LEVEL"

DEFAULT_DB_PATH = "./bracket_index.db"
DEFAULT_CHUNK_SIZE = 1024
DEFAULT_LOG_LEVEL = "INFO"

MIN_CHUNK_SIZE = 1
MAX_CHUNK_SIZE = 1 << 20


@dataclass(frozen=True)
class Settings:
    db_path: str = DEFAULT_DB_PATH
    chunk_size: int = DEFAULT_CHUNK_SIZE
    log_level: str = DEFAULT_LOG_LEVEL

    def __post_init__(self) -> None:
        if not MIN_CHUNK_SIZE <= self.chunk_size <= MAX_CHUNK_SIZE:
            raise ValueError(
                f"chunk_size must be within [{MIN_CHUNK_SIZE}, {MAX_CHUNK_SIZE}], "
                f"got {self.chunk_size}"
            )


def load_settings(env: dict[str, str] | None = None) -> Settings:
    source = os.environ if env is None else env
    return Settings(
        db_path=source.get(ENV_DB_PATH, DEFAULT_DB_PATH),
        chunk_size=int(source.get(ENV_CHUNK_SIZE, str(DEFAULT_CHUNK_SIZE))),
        log_level=source.get(ENV_LOG_LEVEL, DEFAULT_LOG_LEVEL),
    )
