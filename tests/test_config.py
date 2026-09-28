"""Tests for central configuration and env overrides."""

from __future__ import annotations

import importlib

from entity_resolution import config as config_module
from entity_resolution.clustering import SolverConfig
from entity_resolution.similarity import SimilarityConfig


def test_config_builds_engine_subconfigs() -> None:
    cfg = config_module.AppConfig(
        threshold=0.5,
        solver_mode="exact",
        max_exact_partitions=123,
        hard_attribute_keys=frozenset({"reg_id"}),
    )
    sim = cfg.similarity_config()
    sol = cfg.solver_config()
    assert isinstance(sim, SimilarityConfig)
    assert sim.threshold == 0.5
    assert sim.hard_attribute_keys == frozenset({"reg_id"})
    assert isinstance(sol, SolverConfig)
    assert sol.mode == "exact"
    assert sol.max_exact_partitions == 123


def test_env_overrides(monkeypatch) -> None:
    monkeypatch.setenv("ER_THRESHOLD", "0.33")
    monkeypatch.setenv("ER_SOLVER_MODE", "exact")
    monkeypatch.setenv("ER_MAX_EXACT_PARTITIONS", "7")
    monkeypatch.setenv("ER_DB_PATH", ":memory:")
    monkeypatch.setenv("ER_LOG_DIR", "/tmp/er-logs")
    importlib.reload(config_module)
    cfg = config_module.AppConfig()
    assert cfg.threshold == 0.33
    assert cfg.solver_mode == "exact"
    assert cfg.max_exact_partitions == 7
    assert cfg.db_path == ":memory:"
    assert cfg.log_dir == "/tmp/er-logs"
    # Restore the process default singleton for other modules.
    importlib.reload(config_module)
