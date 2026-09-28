"""Central configuration.

All tunables have safe local defaults and can be overridden through
environment variables so no secret or environment-specific value is hardcoded
in source. The corpus *spec* itself (record/constraint/alias shape) lives in
:mod:`entity_resolution.models`; this module only configures the engine.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .clustering import SolverConfig
from .similarity import SimilarityConfig


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw is not None else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw is not None else default


@dataclass(frozen=True)
class AppConfig:
    # In-memory by default so merely importing the ASGI module has no
    # filesystem side effect; set ER_DB_PATH for a persistent database.
    db_path: str = field(default_factory=lambda: os.environ.get("ER_DB_PATH", ":memory:"))
    log_dir: str = field(default_factory=lambda: os.environ.get("ER_LOG_DIR", "logs"))
    threshold: float = field(default_factory=lambda: _env_float("ER_THRESHOLD", 0.75))
    solver_mode: str = field(default_factory=lambda: os.environ.get("ER_SOLVER_MODE", "auto"))
    max_exact_partitions: int = field(
        default_factory=lambda: _env_int("ER_MAX_EXACT_PARTITIONS", 50_000)
    )
    # Attribute keys whose disagreement vetoes a name-based candidate.
    hard_attribute_keys: frozenset[str] = frozenset()

    def similarity_config(self) -> SimilarityConfig:
        return SimilarityConfig(
            threshold=self.threshold,
            hard_attribute_keys=self.hard_attribute_keys,
        )

    def solver_config(self) -> SolverConfig:
        return SolverConfig(
            mode=self.solver_mode,
            max_exact_partitions=self.max_exact_partitions,
        )


DEFAULT_CONFIG = AppConfig()
