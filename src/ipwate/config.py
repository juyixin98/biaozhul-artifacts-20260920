"""Configuration loading.

Only a small, documented subset of the YAML contract may be overridden via the
API; this loader owns validation so the estimator core never reads raw dicts.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

import yaml

from .errors import ValidationError

Estimand = Literal["ate", "att", "atu"]
WeightType = Literal["stabilized", "ht"]

_THIS_DIR = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = _THIS_DIR / "configs" / "default.yaml"

_ALLOWED_API_OVERRIDES = {
    "estimand",
    "weight_type",
    "crossfit.n_splits",
    "crossfit.seed",
    "weights.clipping.enabled",
}


@dataclass(frozen=True)
class CrossfitConfig:
    n_splits: int = 5
    stratify: bool = True
    seed: int = 20260927


@dataclass(frozen=True)
class ModelConfig:
    family: str = "logistic_ridge"
    ridge_lambda: float = 1e-3
    max_iter: int = 100
    tol: float = 1e-8


@dataclass(frozen=True)
class ClippingConfig:
    enabled: bool = False
    profile: str = "v1_fixed_p0p02_p0p98"
    method: str = "fixed_propensity_bounds"
    lower: float = 0.02
    upper: float = 0.98


@dataclass(frozen=True)
class WeightConfig:
    clipping: ClippingConfig = field(default_factory=ClippingConfig)
    positivity_eps: float = 1e-6


@dataclass(frozen=True)
class DiagnosticsConfig:
    min_ess_fraction_warn: float = 0.10
    min_ess_fraction_reject: float = 0.02
    max_weight_warn: float = 20.0
    max_weight_reject: float = 100.0
    overlap_low_quantile: float = 0.05
    overlap_high_quantile: float = 0.95
    calibration_slope_bounds: tuple[float, float] = (0.5, 1.5)
    hosmer_lemeshow_p_min: float = 0.01
    hosmer_lemeshow_groups: int = 10
    calibration_min_n: int = 200


@dataclass(frozen=True)
class ApiConfig:
    max_observations: int = 100_000
    max_covariates: int = 100


@dataclass(frozen=True)
class StorageConfig:
    db_path: str = "data/ipw_runs.sqlite3"


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"


@dataclass(frozen=True)
class AppConfig:
    estimand: Estimand = "ate"
    weight_type: WeightType = "stabilized"
    crossfit: CrossfitConfig = field(default_factory=CrossfitConfig)
    model: ModelConfig = field(default_factory=ModelConfig)
    weights: WeightConfig = field(default_factory=WeightConfig)
    diagnostics: DiagnosticsConfig = field(default_factory=DiagnosticsConfig)
    api: ApiConfig = field(default_factory=ApiConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    source_path: str | None = None

    def with_api_overrides(self, overrides: dict[str, Any]) -> "AppConfig":
        """Apply the whitelisted subset of overrides, rejecting anything else."""
        unknown = sorted(set(overrides) - _ALLOWED_API_OVERRIDES)
        if unknown:
            raise ValidationError(
                "Unsupported contract override(s)",
                details={"unknown": unknown, "allowed": sorted(_ALLOWED_API_OVERRIDES)},
            )
        cfg = self
        if "estimand" in overrides:
            value = overrides["estimand"]
            if value not in ("ate", "att", "atu"):
                raise ValidationError("estimand must be one of ate|att|atu")
            cfg = replace(cfg, estimand=value)
        if "weight_type" in overrides:
            value = overrides["weight_type"]
            if value not in ("stabilized", "ht"):
                raise ValidationError("weight_type must be stabilized|ht")
            cfg = replace(cfg, weight_type=value)
        if "crossfit.n_splits" in overrides:
            value = int(overrides["crossfit.n_splits"])
            if not 2 <= value <= 20:
                raise ValidationError("crossfit.n_splits must be in [2, 20]")
            cfg = replace(cfg, crossfit=replace(cfg.crossfit, n_splits=value))
        if "crossfit.seed" in overrides:
            cfg = replace(cfg, crossfit=replace(cfg.crossfit, seed=int(overrides["crossfit.seed"])))
        if "weights.clipping.enabled" in overrides:
            enabled = bool(overrides["weights.clipping.enabled"])
            cfg = replace(
                cfg,
                weights=replace(
                    cfg.weights, clipping=replace(cfg.weights.clipping, enabled=enabled)
                ),
            )
        return cfg


def _coerce_diagnostics(raw: dict[str, Any]) -> DiagnosticsConfig:
    slope = raw.get("calibration_slope_bounds", [0.5, 1.5])
    return DiagnosticsConfig(
        min_ess_fraction_warn=float(raw["min_ess_fraction_warn"]),
        min_ess_fraction_reject=float(raw["min_ess_fraction_reject"]),
        max_weight_warn=float(raw["max_weight_warn"]),
        max_weight_reject=float(raw["max_weight_reject"]),
        overlap_low_quantile=float(raw["overlap_low_quantile"]),
        overlap_high_quantile=float(raw["overlap_high_quantile"]),
        calibration_slope_bounds=(float(slope[0]), float(slope[1])),
        hosmer_lemeshow_p_min=float(raw["hosmer_lemeshow_p_min"]),
        hosmer_lemeshow_groups=int(raw["hosmer_lemeshow_groups"]),
        calibration_min_n=int(raw["calibration_min_n"]),
    )


def load_config(path: str | os.PathLike[str] | None = None) -> AppConfig:
    """Load and validate the YAML config; missing file -> built-in defaults."""
    path = Path(path) if path else Path(os.environ.get("IPW_CONFIG_PATH", DEFAULT_CONFIG_PATH))
    if not path.exists():
        return AppConfig(source_path=None)
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ValidationError(f"Invalid YAML config: {exc}") from exc

    crossfit_raw = raw.get("crossfit", {})
    model_raw = raw.get("model", {})
    weight_raw = raw.get("weights", {})
    clip_raw = weight_raw.get("clipping", {})
    return AppConfig(
        estimand=raw.get("estimand", "ate"),
        weight_type=raw.get("weight_type", "stabilized"),
        crossfit=CrossfitConfig(
            n_splits=int(crossfit_raw.get("n_splits", 5)),
            stratify=bool(crossfit_raw.get("stratify", True)),
            seed=int(crossfit_raw.get("seed", 20260927)),
        ),
        model=ModelConfig(
            family=model_raw.get("family", "logistic_ridge"),
            ridge_lambda=float(model_raw.get("ridge_lambda", 1e-3)),
            max_iter=int(model_raw.get("max_iter", 100)),
            tol=float(model_raw.get("tol", 1e-8)),
        ),
        weights=WeightConfig(
            clipping=ClippingConfig(
                enabled=bool(clip_raw.get("enabled", False)),
                profile=clip_raw.get("profile", "v1_fixed_p0p02_p0p98"),
                method=clip_raw.get("method", "fixed_propensity_bounds"),
                lower=float(clip_raw.get("lower", 0.02)),
                upper=float(clip_raw.get("upper", 0.98)),
            ),
            positivity_eps=float(weight_raw.get("positivity_eps", 1e-6)),
        ),
        diagnostics=_coerce_diagnostics(raw.get("diagnostics", {})),
        api=ApiConfig(**raw.get("api", {})),
        storage=StorageConfig(**raw.get("storage", {})),
        logging=LoggingConfig(**raw.get("logging", {})),
        source_path=str(path),
    )
