"""Environment-driven configuration. No secrets are needed for this project."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    db_path: str
    small_sample_threshold: int
    rare_event_threshold: int
    max_transactions: int
    max_items_per_transaction: int
    redact_pii: bool

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path}"


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw is not None and raw.strip() else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def get_settings() -> Settings:
    return Settings(
        db_path=os.environ.get("RULE_AUDIT_DB_PATH", os.path.join("data", "audit.db")),
        small_sample_threshold=_env_int("RULE_AUDIT_SMALL_SAMPLE_N", 30),
        rare_event_threshold=_env_int("RULE_AUDIT_RARE_EVENT_COUNT", 5),
        max_transactions=_env_int("RULE_AUDIT_MAX_TRANSACTIONS", 100_000),
        max_items_per_transaction=_env_int("RULE_AUDIT_MAX_ITEMS_PER_TXN", 1_000),
        redact_pii=_env_bool("RULE_AUDIT_REDACT_PII", True),
    )
