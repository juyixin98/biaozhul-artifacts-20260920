"""Configuration loading.

Settings are read from ``config/default.yaml`` (override path through the
``ROOT_ISOLATION_CONFIG`` environment variable). Budgets are parsed once at
startup and exposed as an immutable :class:`Settings` value object — the kernel
never mutates configuration.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "default.yaml"


@dataclass(frozen=True)
class KernelSettings:
    precision_dps: int
    max_degree: int
    max_coefficients: int
    max_bisections: int
    max_sturm_length: int
    max_coeff_bits: int


@dataclass(frozen=True)
class HttpSettings:
    max_body_bytes: int


@dataclass(frozen=True)
class ServiceSettings:
    host: str
    port: int
    log_level: str


@dataclass(frozen=True)
class Settings:
    service: ServiceSettings
    kernel: KernelSettings
    http: HttpSettings


def _load_raw(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"configuration file {path} must contain a mapping")
    return data


def load_settings(path: str | os.PathLike[str] | None = None) -> Settings:
    """Load and validate settings, failing fast on malformed budgets."""
    config_path = Path(path) if path else Path(
        os.environ.get("ROOT_ISOLATION_CONFIG", DEFAULT_CONFIG_PATH)
    )
    raw = _load_raw(config_path)

    service_raw = raw["service"]
    kernel_raw = raw["kernel"]
    http_raw = raw["http"]

    kernel = KernelSettings(
        precision_dps=int(kernel_raw["precision_dps"]),
        max_degree=int(kernel_raw["max_degree"]),
        max_coefficients=int(kernel_raw["max_coefficients"]),
        max_bisections=int(kernel_raw["max_bisections"]),
        max_sturm_length=int(kernel_raw["max_sturm_length"]),
        max_coeff_bits=int(kernel_raw["max_coeff_bits"]),
    )
    if kernel.precision_dps < 30:
        raise ValueError("kernel.precision_dps must be >= 30")
    if kernel.max_degree < 1:
        raise ValueError("kernel.max_degree must be >= 1")
    if kernel.max_bisections < 100:
        raise ValueError("kernel.max_bisections must be >= 100")

    return Settings(
        service=ServiceSettings(
            host=str(service_raw["host"]),
            port=int(service_raw["port"]),
            log_level=str(service_raw["log_level"]),
        ),
        kernel=kernel,
        http=HttpSettings(max_body_bytes=int(http_raw["max_body_bytes"])),
    )
