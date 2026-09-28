"""Application configuration loaded from config/settings.yaml.

Configuration is loaded once at import time but ``load_config`` can be called
again (e.g. by tests pointing at a temporary directory). Nothing here performs
network or database access.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "settings.yaml"


@dataclass(frozen=True)
class EstimationSettings:
    cluster_adjustment: str = "crv1"
    min_clusters: int = 2


@dataclass(frozen=True)
class ReproSettings:
    fixture_seed: int = 20260927
    rng_algo: str = "PCG64"


@dataclass(frozen=True)
class AppConfig:
    service_name: str
    version: str
    core_version: str
    db_path: Path
    log_path: Path
    log_level: str
    estimation: EstimationSettings = field(default_factory=EstimationSettings)
    reproducibility: ReproSettings = field(default_factory=ReproSettings)

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path.resolve()}"


def _resolve_path(value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else REPO_ROOT / p


def load_config(path: str | os.PathLike[str] | None = None) -> AppConfig:
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    with open(cfg_path, "r", encoding="utf-8") as fh:
        raw: dict[str, Any] = yaml.safe_load(fh)

    est = raw.get("estimation", {})
    repro = raw.get("reproducibility", {})
    svc = raw.get("service", {})
    db = raw.get("database", {})
    log = raw.get("logging", {})

    return AppConfig(
        service_name=svc.get("name", "did-panel-service"),
        version=svc.get("version", "0.0.0"),
        core_version=svc.get("core_version", "did-core"),
        db_path=_resolve_path(db.get("path", "data/did_ledger.db")),
        log_path=_resolve_path(log.get("path", "logs/service.log")),
        log_level=log.get("level", "INFO"),
        estimation=EstimationSettings(
            cluster_adjustment=est.get("cluster_adjustment", "crv1"),
            min_clusters=int(est.get("min_clusters", 2)),
        ),
        reproducibility=ReproSettings(
            fixture_seed=int(repro.get("fixture_seed", 20260927)),
            rng_algo=repro.get("rng_algo", "PCG64"),
        ),
    )


CONFIG = load_config()
