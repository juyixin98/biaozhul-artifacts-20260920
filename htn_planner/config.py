"""Runtime configuration, sourced from environment variables.

No secrets are required; the service is fully local with synthetic inputs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .core.engine import Bounds

API_V1 = "/api/v1"


@dataclass(frozen=True)
class Settings:
    db_path: str = "data/htn_evidence.db"
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "INFO"
    bounds: Bounds = field(default_factory=Bounds)
    service_name: str = "htn-planner"
    service_version: str = "0.1.0"

    @staticmethod
    def from_env() -> "Settings":
        return Settings(
            db_path=os.environ.get("HTN_DB_PATH", "data/htn_evidence.db"),
            host=os.environ.get("HTN_HOST", "127.0.0.1"),
            port=int(os.environ.get("HTN_PORT", "8000")),
            log_level=os.environ.get("HTN_LOG_LEVEL", "INFO").upper(),
            bounds=Bounds(
                max_depth=int(os.environ.get("HTN_MAX_DEPTH", "12")),
                max_expansions=int(os.environ.get("HTN_MAX_EXPANSIONS", "500")),
                max_actions=int(os.environ.get("HTN_MAX_ACTIONS", "100")),
                max_search_nodes=int(os.environ.get("HTN_MAX_SEARCH_NODES", "5000")),
                max_evidence=int(os.environ.get("HTN_MAX_EVIDENCE", "60")),
                max_steps=int(os.environ.get("HTN_MAX_STEPS", "120")),
            ),
        )
