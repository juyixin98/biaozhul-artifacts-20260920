"""Runtime configuration.

Settings come from environment variables with ``CSP_`` prefixes and have
sane local defaults; nothing here requires production credentials.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    database_path: str = field(
        default_factory=lambda: os.environ.get("CSP_DATABASE_PATH", "csp_evidence.db")
    )
    default_max_nodes: int = field(
        default_factory=lambda: int(os.environ.get("CSP_MAX_NODES", "100000"))
    )
    default_max_backtracks: int = field(
        default_factory=lambda: int(os.environ.get("CSP_MAX_BACKTRACKS", "100000"))
    )
    max_variables: int = field(
        default_factory=lambda: int(os.environ.get("CSP_MAX_VARIABLES", "64"))
    )
    max_domain_size: int = field(
        default_factory=lambda: int(os.environ.get("CSP_MAX_DOMAIN_SIZE", "256"))
    )
    log_level: str = field(
        default_factory=lambda: os.environ.get("CSP_LOG_LEVEL", "INFO")
    )


def get_settings() -> Settings:
    return Settings()
