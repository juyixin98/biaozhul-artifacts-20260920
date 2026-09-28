"""Application configuration loaded from environment variables.

Everything defaults to local, self-contained values so the service runs
out of the box with only synthetic fixtures.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw) if raw else default


@dataclass(frozen=True)
class Settings:
    db_path: Path
    corpus_dir: Path
    page_size_default: int
    page_size_max: int

    @staticmethod
    def from_env() -> "Settings":
        page_max = int(os.environ.get("COLLSVC_PAGE_SIZE_MAX", "200"))
        return Settings(
            db_path=_env_path("COLLSVC_DB_PATH", PROJECT_ROOT / "data" / "collsvc.db"),
            corpus_dir=_env_path("COLLSVC_CORPUS_DIR", PROJECT_ROOT / "data" / "corpus"),
            page_size_default=int(os.environ.get("COLLSVC_PAGE_SIZE_DEFAULT", "50")),
            page_size_max=page_max,
        )
