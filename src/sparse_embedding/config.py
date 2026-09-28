"""Independent configuration layer.

Everything that can change the *numerical* behaviour of the service lives here
and is represented by an immutable, validated value object. Nothing in the
computational core reads environment variables or mutates global state.
"""

from __future__ import annotations

import enum
import json
import os
from dataclasses import asdict, dataclass, field
from typing import Any


class ClipMode(str, enum.Enum):
    """How gradient clipping is interpreted.

    The mode is deliberately a closed enum so a caller cannot accidentally mix
    global and row-wise semantics: each request must name exactly one.
    """

    NONE = "none"
    GLOBAL = "global"
    ROW = "row"


class OptimizerName(str, enum.Enum):
    SGD = "sgd"
    SGD_MOMENTUM = "sgd_momentum"


@dataclass(frozen=True)
class TableSpec:
    """Static description of one embedding table."""

    num_rows: int
    dim: int

    def __post_init__(self) -> None:
        if self.num_rows <= 0:
            raise ValueError(f"num_rows must be positive, got {self.num_rows}")
        if self.dim <= 0:
            raise ValueError(f"dim must be positive, got {self.dim}")


@dataclass(frozen=True)
class OptimizerConfig:
    """Optimizer hyper-parameters.

    ``step_zero_rows`` is the explicit rule for rows that are present in a
    batch but aggregate to an all-zero gradient, and for rows that are not
    touched at all. Both classes of row NEVER take an optimizer step. The flag
    is retained for documentation/assertions and to make the rule auditable.
    """

    name: OptimizerName = OptimizerName.SGD_MOMENTUM
    lr: float = 0.01
    momentum: float = 0.9
    weight_decay: float = 0.0
    step_zero_rows: bool = False  # must stay False; zero gradients do not step

    def __post_init__(self) -> None:
        if not isinstance(self.name, OptimizerName):
            raise TypeError(f"name must be OptimizerName, got {type(self.name)!r}")
        if self.lr <= 0:
            raise ValueError(f"lr must be > 0, got {self.lr}")
        if not 0.0 <= self.momentum < 1.0:
            raise ValueError(f"momentum must be in [0, 1), got {self.momentum}")
        if self.weight_decay < 0.0:
            raise ValueError(f"weight_decay must be >= 0, got {self.weight_decay}")
        if self.name is OptimizerName.SGD and self.momentum != 0.0:
            raise ValueError("plain sgd requires momentum=0 (use sgd_momentum otherwise)")
        if self.step_zero_rows:
            raise ValueError(
                "step_zero_rows must be False: rows with a zero aggregated gradient "
                "and untouched rows never take an optimizer step"
            )


@dataclass(frozen=True)
class ClipConfig:
    """Gradient clipping declaration.

    ``mode`` and ``max_norm`` are interpreted together and never mixed:

    * ``GLOBAL``: one scale factor shared by every row, computed from the
      Frobenius norm of the whole aggregated sparse batch.
    * ``ROW``: a separate scale factor per row, computed from that row's norm.
    * ``NONE``: no clipping regardless of ``max_norm``.
    """

    mode: ClipMode = ClipMode.NONE
    max_norm: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.mode, ClipMode):
            raise TypeError(f"mode must be ClipMode, got {type(self.mode)!r}")
        if self.mode is not ClipMode.NONE:
            if self.max_norm is None or self.max_norm <= 0:
                raise ValueError(
                    f"clipping mode {self.mode.value} requires a positive max_norm, "
                    f"got {self.max_norm}"
                )


@dataclass(frozen=True)
class ServiceConfig:
    """Top-level configuration for the optimizer service."""

    table: TableSpec
    optimizer: OptimizerConfig = field(default_factory=OptimizerConfig)
    clip: ClipConfig = field(default_factory=ClipConfig)
    state_dir: str = "var/state"
    seed: int = 1234

    def __post_init__(self) -> None:
        if not isinstance(self.table, TableSpec):
            raise TypeError("table must be a TableSpec")
        if not isinstance(self.optimizer, OptimizerConfig):
            raise TypeError("optimizer must be an OptimizerConfig")
        if not isinstance(self.clip, ClipConfig):
            raise TypeError("clip must be a ClipConfig")

    # -- (de)serialisation -------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ServiceConfig":
        table = TableSpec(**raw["table"])
        opt_raw = dict(raw.get("optimizer", {}))
        opt_raw["name"] = OptimizerName(opt_raw.get("name", OptimizerName.SGD_MOMENTUM.value))
        optimizer = OptimizerConfig(**opt_raw)
        clip_raw = dict(raw.get("clip", {}))
        if "mode" in clip_raw:
            clip_raw["mode"] = ClipMode(clip_raw["mode"])
        clip = ClipConfig(**clip_raw)
        return cls(
            table=table,
            optimizer=optimizer,
            clip=clip,
            state_dir=raw.get("state_dir", "var/state"),
            seed=int(raw.get("seed", 1234)),
        )

    @classmethod
    def load(cls, path: str) -> "ServiceConfig":
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))

    def dump(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=2, sort_keys=True)
            fh.write("\n")


def env_config(default_state_dir: str = "var/state") -> ServiceConfig:
    """Build config used by the service entry point.

    Kept at the edge (never imported by the numerical core) so the core stays
    pure. Reads ``SPARSE_EMB_CONFIG`` (path to a JSON config) and a couple of
    explicit overrides.
    """

    path = os.environ.get("SPARSE_EMB_CONFIG")
    if path:
        return ServiceConfig.load(path)

    num_rows = int(os.environ.get("SPARSE_EMB_ROWS", "100000"))
    dim = int(os.environ.get("SPARSE_EMB_DIM", "16"))
    return ServiceConfig(
        table=TableSpec(num_rows=num_rows, dim=dim),
        state_dir=os.environ.get("SPARSE_EMB_STATE_DIR", default_state_dir),
    )
