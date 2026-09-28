"""Configuration layer.

All tunables of the backend live here so the kernel, cache and service
share one explicit, testable source of truth. Values can be overridden
through environment variables (prefix ``TOEPLITZ_``) for the service,
or by constructing a ``ToeplitzConfig`` directly in tests.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .errors import ConfigurationError

_VALID_REAL_DTYPES = ("float64", "float32")
_VALID_COMPLEX_DTYPES = ("complex128", "complex64")


@dataclass(frozen=True)
class ToeplitzConfig:
    """Immutable backend configuration.

    Attributes:
        real_dtype: output precision for the real path ("float64" default).
        complex_dtype: output precision for the complex path ("complex128" default).
        pad_to_power_of_two: if True, round the circulant embedding length up to
            the next power of two (FFT speed). The length is *always* >= m + n - 1
            so circular aliasing is impossible either way.
        cache_maxsize: maximum number of prepared operator spectra kept in the LRU cache.
        consistency_rtol / consistency_atol: tolerances for the c[0] == r[0] check.
        err_rtol / err_atol: default tolerances used by the evidence layer when
            judging FFT results against a reference.
    """

    real_dtype: str = "float64"
    complex_dtype: str = "complex128"
    pad_to_power_of_two: bool = False
    cache_maxsize: int = 64
    consistency_rtol: float = 1e-9
    consistency_atol: float = 1e-12
    err_rtol: float = 1e-9
    err_atol: float = 1e-9

    def __post_init__(self) -> None:
        if self.real_dtype not in _VALID_REAL_DTYPES:
            raise ConfigurationError(
                f"real_dtype must be one of {_VALID_REAL_DTYPES}, got {self.real_dtype!r}"
            )
        if self.complex_dtype not in _VALID_COMPLEX_DTYPES:
            raise ConfigurationError(
                f"complex_dtype must be one of {_VALID_COMPLEX_DTYPES}, got {self.complex_dtype!r}"
            )
        if self.cache_maxsize < 1:
            raise ConfigurationError("cache_maxsize must be >= 1")

    @classmethod
    def from_env(cls) -> "ToeplitzConfig":
        """Build a config from TOEPLITZ_* environment variables (defaults otherwise)."""
        env = os.environ
        return cls(
            real_dtype=env.get("TOEPLITZ_REAL_DTYPE", "float64"),
            complex_dtype=env.get("TOEPLITZ_COMPLEX_DTYPE", "complex128"),
            pad_to_power_of_two=env.get("TOEPLITZ_PAD_POW2", "0") in ("1", "true", "yes"),
            cache_maxsize=int(env.get("TOEPLITZ_CACHE_MAXSIZE", "64")),
        )
