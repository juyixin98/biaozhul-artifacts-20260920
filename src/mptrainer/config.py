"""Configuration layer: validated trainer config, JSON round-trip."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .tensors import resolve_low_dtype


class ConfigError(ValueError):
    """Raised for invalid trainer configuration (maps to HTTP 422)."""


@dataclass(frozen=True)
class TrainerConfig:
    layer_sizes: list[int] = field(default_factory=lambda: [8, 16, 1])
    seed: int = 1234
    base_lr: float = 0.01
    lr_decay: float = 0.0          # lr = base_lr / (1 + lr_decay * optimizer_step)
    momentum: float = 0.9
    accum_steps: int = 1           # gradient-accumulation window (micro-steps)
    low_dtype: str = "float16"
    init_scale: float = 1024.0
    growth_factor: float = 2.0
    backoff_factor: float = 0.5
    growth_interval: int = 2000

    def validate(self) -> "TrainerConfig":
        if len(self.layer_sizes) < 2 or any(s < 1 for s in self.layer_sizes):
            raise ConfigError("layer_sizes must be >= 2 positive integers")
        if self.base_lr <= 0:
            raise ConfigError("base_lr must be > 0")
        if self.lr_decay < 0:
            raise ConfigError("lr_decay must be >= 0")
        if not 0.0 <= self.momentum < 1.0:
            raise ConfigError("momentum must be in [0, 1)")
        if self.accum_steps < 1:
            raise ConfigError("accum_steps must be >= 1")
        if self.init_scale < 1.0:
            raise ConfigError("init_scale must be >= 1")
        if not 0.0 < self.backoff_factor < 1.0:
            raise ConfigError("backoff_factor must be in (0, 1)")
        if self.growth_factor <= 1.0:
            raise ConfigError("growth_factor must be > 1")
        if self.growth_interval < 1:
            raise ConfigError("growth_interval must be >= 1")
        try:
            resolve_low_dtype(self.low_dtype)
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
        return self

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "TrainerConfig":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        unknown = set(data) - known
        if unknown:
            raise ConfigError(f"unknown config keys: {sorted(unknown)}")
        return cls(**data).validate()

    @classmethod
    def load(cls, path: str | Path) -> "TrainerConfig":
        return cls.from_dict(json.loads(Path(path).read_text()))

    def dump(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2) + "\n")
