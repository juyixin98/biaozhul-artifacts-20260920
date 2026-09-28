"""Application configuration loaded from environment variables.

All values have safe local defaults so the service starts with zero setup;
nothing here requires a production account or external service.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

SERVICE_NAME = "tensor-backend"
SERVICE_VERSION = "1.0.0"


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        # Fail fast with an explanatory message rather than silently ignoring
        # a malformed operator-supplied value.
        raise ConfigurationError(f"environment variable {name} must be an integer, got {raw!r}")


class ConfigurationError(RuntimeError):
    """Raised when the environment supplies an invalid configuration value."""


@dataclass(frozen=True)
class Settings:
    service_name: str = SERVICE_NAME
    version: str = SERVICE_VERSION
    host: str = "127.0.0.1"
    port: int = 8000
    # Bounds protect the service from pathological requests that would try to
    # allocate enormous buffers (size multiplication overflow is still checked
    # independently in the tensor layer).
    max_ndim: int = 32
    max_elements: int = 100_000_000
    log_uncertainties: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            host=os.environ.get("TENSOR_BACKEND_HOST", "127.0.0.1"),
            port=_env_int("TENSOR_BACKEND_PORT", 8000),
            max_ndim=_env_int("TENSOR_BACKEND_MAX_NDIM", 32),
            max_elements=_env_int("TENSOR_BACKEND_MAX_ELEMENTS", 100_000_000),
            log_uncertainties=os.environ.get("TENSOR_BACKEND_LOG_UNCERTAINTIES", "1") not in ("0", "false", "False", ""),
        )


SETTINGS = Settings.from_env()
