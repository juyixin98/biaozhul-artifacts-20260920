"""Runtime configuration loaded from environment variables.

Kept dependency-free so it can be imported without a running server.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_DB_PATH = str(Path(__file__).resolve().parent.parent / "data" / "finality.db")


class Settings:
    """Process-wide settings."""

    def __init__(
        self,
        db_path: str | None = None,
        chain_id: str | None = None,
        reset_on_start: bool | None = None,
    ) -> None:
        self.db_path: str = db_path if db_path is not None else os.environ.get(
            "FDD_DB_PATH", DEFAULT_DB_PATH
        )
        self.chain_id: str = chain_id if chain_id is not None else os.environ.get(
            "FDD_CHAIN_ID", "demo-chain"
        )
        if reset_on_start is None:
            reset_on_start = os.environ.get("FDD_RESET_ON_START", "false").lower() in (
                "1",
                "true",
                "yes",
            )
        self.reset_on_start: bool = reset_on_start


settings = Settings()
