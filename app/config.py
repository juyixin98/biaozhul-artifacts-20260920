"""Runtime configuration: propagation budgets and service settings.

Budgets are explicit and part of the behavior contract: when a budget is
exceeded the engine marks its results incomplete instead of silently
dropping work (see app.core.engine).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Budget:
    """Hard limits for one ATMS session's propagation.

    max_envs_per_label:       cap on the size of any single node label.
    max_propagation_steps:    cap on total queue iterations per propagation run.
    max_combinations_per_rule: cap on antecedent-environment products expanded
                              for one rule firing.
    """

    max_envs_per_label: int = 64
    max_propagation_steps: int = 10_000
    max_combinations_per_rule: int = 4_096

    def __post_init__(self) -> None:
        for name in (
            "max_envs_per_label",
            "max_propagation_steps",
            "max_combinations_per_rule",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"budget {name} must be >= 1")


@dataclass(frozen=True)
class Settings:
    """Service-level settings, overridable via ATMS_* environment variables."""

    db_path: str = "atms.db"
    budget: Budget = field(default_factory=Budget)
    # When False, assumption/node names are hashed in diagnostic records so
    # sensitive vocabulary never lands in logs (defence-in-depth redaction).
    log_symbol_names: bool = False


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return default if raw is None else int(raw)


def load_settings() -> Settings:
    return Settings(
        db_path=os.environ.get("ATMS_DB_PATH", "atms.db"),
        budget=Budget(
            max_envs_per_label=_int_env("ATMS_MAX_ENVS_PER_LABEL", 64),
            max_propagation_steps=_int_env("ATMS_MAX_PROPAGATION_STEPS", 10_000),
            max_combinations_per_rule=_int_env(
                "ATMS_MAX_COMBINATIONS_PER_RULE", 4_096
            ),
        ),
        log_symbol_names=os.environ.get("ATMS_LOG_SYMBOL_NAMES", "0") == "1",
    )
