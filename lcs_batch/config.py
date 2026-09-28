"""Runtime configuration, overridable via environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass

# Version of the mining kernel algorithm. Stored in the index metadata so a
# persisted index can be rejected when the kernel semantics change.
KERNEL_VERSION = "gsa-lcp/1.0"


@dataclass(frozen=True)
class Settings:
    db_path: str = "./lcs_index.db"
    max_documents: int = 10_000
    max_document_bytes: int = 1_000_000
    # Total encoded symbol count (content + one separator per document).
    # The pure-Python suffix array builder is O(n log n); this ceiling keeps
    # build times reasonable for a local review deployment.
    max_total_symbols: int = 2_000_000
    default_max_candidates: int = 50
    max_candidates_ceiling: int = 1_000

    @staticmethod
    def from_env() -> "Settings":
        return Settings(
            db_path=os.environ.get("LCS_DB_PATH", "./lcs_index.db"),
            max_documents=int(os.environ.get("LCS_MAX_DOCUMENTS", "10000")),
            max_document_bytes=int(os.environ.get("LCS_MAX_DOCUMENT_BYTES", "1000000")),
            max_total_symbols=int(os.environ.get("LCS_MAX_TOTAL_SYMBOLS", "2000000")),
            default_max_candidates=int(os.environ.get("LCS_DEFAULT_MAX_CANDIDATES", "50")),
            max_candidates_ceiling=int(os.environ.get("LCS_MAX_CANDIDATES_CEILING", "1000")),
        )
