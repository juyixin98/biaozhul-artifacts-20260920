"""Configuration loading (JSON file under ``config/``, overridable by env vars)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT / "config" / "service.json"


@dataclass(frozen=True)
class ServiceConfig:
    max_obs: int
    db_path: str
    log_level: str
    log_raw_data: bool
    data_hash_prefix_len: int
    default_cov_type: str
    significance: float
    weak_rule: str
    rank_rcond: float
    vif_warn: float
    hc_small_sample: bool
    overid_significance: float
    endogeneity_significance: float

    @property
    def absolute_db_path(self) -> Path:
        p = Path(self.db_path)
        return p if p.is_absolute() else ROOT / p


def load_config(path: str | os.PathLike[str] | None = None) -> ServiceConfig:
    cfg_path = Path(path) if path else Path(os.environ.get("TWOSLS_CONFIG", DEFAULT_CONFIG_PATH))
    with open(cfg_path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)

    svc = raw.get("service", {})
    est = raw.get("estimation", {})
    diag = raw.get("diagnostics", {})

    def pick(section: dict, key: str, env: str, cast, default):
        val = os.environ.get(env)
        if val is not None:
            return cast(val)
        return section.get(key, default)

    return ServiceConfig(
        max_obs=pick(svc, "max_obs", "TWOSLS_MAX_OBS", int, 200_000),
        db_path=pick(svc, "db_path", "TWOSLS_DB_PATH", str, ".state/twosls.db"),
        log_level=pick(svc, "log_level", "TWOSLS_LOG_LEVEL", str, "INFO"),
        log_raw_data=False if os.environ.get("TWOSLS_LOG_RAW_DATA") is None
        else os.environ["TWOSLS_LOG_RAW_DATA"].lower() in {"1", "true", "yes"},
        data_hash_prefix_len=pick(svc, "data_hash_prefix_len", "TWOSLS_HASH_PREFIX", int, 12),
        default_cov_type=pick(est, "default_cov_type", "TWOSLS_COV", str, "conventional"),
        significance=pick(est, "significance", "TWOSLS_ALPHA", float, 0.05),
        weak_rule=pick(est, "weak_rule", "TWOSLS_WEAK_RULE", str, "stock_yogo_10"),
        rank_rcond=pick(est, "rank_rcond", "TWOSLS_RANK_RCOND", float, 1e-9),
        vif_warn=pick(est, "vif_warn", "TWOSLS_VIF_WARN", float, 30.0),
        hc_small_sample=pick(est, "hc_small_sample", "TWOSLS_HC_SS", _as_bool, True),
        overid_significance=pick(diag, "overid_significance", "TWOSLS_OVERID_ALPHA", float, 0.05),
        endogeneity_significance=pick(
            diag, "endogeneity_significance", "TWOSLS_ENDO_ALPHA", float, 0.05
        ),
    )


def _as_bool(v: object) -> bool:
    return str(v).lower() in {"1", "true", "yes"}
