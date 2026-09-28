"""Configuration layer. Values overridable via CSP_* environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    db_path: str = "csp_evidence.db"
    log_dir: str = "logs"
    default_max_nodes: int = 10_000
    default_max_propagation_steps: int = 100_000
    enumeration_max_assignments: int = 2_000_000

    @staticmethod
    def from_env() -> "Settings":
        env = os.environ
        return Settings(
            db_path=env.get("CSP_DB_PATH", "csp_evidence.db"),
            log_dir=env.get("CSP_LOG_DIR", "logs"),
            default_max_nodes=int(env.get("CSP_MAX_NODES", "10000")),
            default_max_propagation_steps=int(env.get("CSP_MAX_PROP_STEPS", "100000")),
            enumeration_max_assignments=int(env.get("CSP_ENUM_MAX_ASSIGNMENTS", "2000000")),
        )
