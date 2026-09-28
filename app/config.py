"""Runtime configuration.

All values can be overridden through environment variables so the service
runs locally without any external accounts or production data.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from app import __version__

APP_VERSION = __version__
# Bump whenever the on-disk index serialization changes; stored indexes
# whose version does not match are rebuilt instead of trusted.
INDEX_FORMAT_VERSION = 1


@dataclass(frozen=True)
class Settings:
    db_path: str = "./lcs_service.db"
    max_documents: int = 512
    max_document_bytes: int = 1_000_000
    max_total_bytes: int = 8_000_000
    max_candidates_cap: int = 256
    default_max_candidates: int = 32
    occurrence_cap: int = 1000


def load_settings() -> Settings:
    defaults = Settings()
    return Settings(
        db_path=os.environ.get("LCS_DB_PATH", defaults.db_path),
        max_documents=int(os.environ.get("LCS_MAX_DOCUMENTS", defaults.max_documents)),
        max_document_bytes=int(
            os.environ.get("LCS_MAX_DOCUMENT_BYTES", defaults.max_document_bytes)
        ),
        max_total_bytes=int(
            os.environ.get("LCS_MAX_TOTAL_BYTES", defaults.max_total_bytes)
        ),
        max_candidates_cap=int(
            os.environ.get("LCS_MAX_CANDIDATES_CAP", defaults.max_candidates_cap)
        ),
        default_max_candidates=int(
            os.environ.get("LCS_DEFAULT_MAX_CANDIDATES", defaults.default_max_candidates)
        ),
        occurrence_cap=int(os.environ.get("LCS_OCCURRENCE_CAP", defaults.occurrence_cap)),
    )
