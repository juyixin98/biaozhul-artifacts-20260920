"""Configuration layer: validated, immutable dataclasses.

The core depends only on NumPy; validation is explicit (no pydantic here) so
the training core stays importable without the service stack.  The API layer
maps its pydantic schemas onto these dataclasses.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .errors import AmpTrainError, ErrorCode

MASTER_DTYPE = np.float32
# float32 is included deliberately as the *full-precision control*: tests run
# identical amplified inputs through float16 vs float32 to contrast skipping
# behaviour against an uninterrupted run.  (bfloat16 is not exposed by every
# NumPy build and is intentionally not advertised; float16 is the
# overflow-driving reduced precision used throughout this project.)
SUPPORTED_LOWP_DTYPES = ("float16", "float32")
SUPPORTED_ACTIVATIONS = ("relu", "tanh")
SUPPORTED_SCHEDULES = ("constant", "step")


def _fail(message: str, **detail) -> None:
    raise AmpTrainError(ErrorCode.CONFIG_INVALID, message, detail=detail)


@dataclass(frozen=True)
class PrecisionConfig:
    """Which dtypes play which role.  The core constraint of this project:

    master weights, low-precision forward parameters and optimizer state are
    *separate* tensors with *separate* dtypes.
    """

    lowp_dtype: str = "float16"
    master_dtype: str = "float32"

    def __post_init__(self) -> None:
        if self.lowp_dtype not in SUPPORTED_LOWP_DTYPES:
            _fail(
                f"unsupported lowp_dtype '{self.lowp_dtype}'",
                supported=list(SUPPORTED_LOWP_DTYPES),
            )
        if self.master_dtype != "float32":
            _fail("master weights must be float32", master_dtype=self.master_dtype)


@dataclass(frozen=True)
class ScalerConfig:
    """Dynamic loss scaling hyperparameters."""

    init_scale: float = 128.0
    growth_factor: float = 2.0
    backoff_factor: float = 0.5
    growth_interval: int = 2000
    min_scale: float = 1.0

    def __post_init__(self) -> None:
        if not (self.init_scale > 0 and np.isfinite(self.init_scale)):
            _fail("init_scale must be a positive finite number", init_scale=self.init_scale)
        if not self.growth_factor > 1.0:
            _fail("growth_factor must be > 1", growth_factor=self.growth_factor)
        if not 0.0 < self.backoff_factor < 1.0:
            _fail("backoff_factor must be in (0, 1)", backoff_factor=self.backoff_factor)
        if not self.growth_interval >= 1:
            _fail("growth_interval must be >= 1", growth_interval=self.growth_interval)
        if not 0.0 < self.min_scale <= self.init_scale:
            _fail(
                "min_scale must be in (0, init_scale]",
                min_scale=self.min_scale,
                init_scale=self.init_scale,
            )


@dataclass(frozen=True)
class AccumulationConfig:
    """Gradient accumulation: one optimizer step commits ``micro_batches``
    micro-batches atomically; any overflow discards the whole window."""

    micro_batches: int = 1

    def __post_init__(self) -> None:
        if not self.micro_batches >= 1:
            _fail("micro_batches must be >= 1", micro_batches=self.micro_batches)


@dataclass(frozen=True)
class OptimizerConfig:
    """SGD with momentum, operating on fp32 master weights only."""

    lr: float = 0.01
    momentum: float = 0.9
    weight_decay: float = 0.0
    schedule: str = "constant"  # "constant" | "step"
    step_size: int = 10
    gamma: float = 0.5

    def __post_init__(self) -> None:
        if not (self.lr > 0 and np.isfinite(self.lr)):
            _fail("lr must be a positive finite number", lr=self.lr)
        if not 0.0 <= self.momentum < 1.0:
            _fail("momentum must be in [0, 1)", momentum=self.momentum)
        if not self.weight_decay >= 0.0:
            _fail("weight_decay must be >= 0", weight_decay=self.weight_decay)
        if self.schedule not in SUPPORTED_SCHEDULES:
            _fail(
                f"unsupported schedule '{self.schedule}'",
                supported=list(SUPPORTED_SCHEDULES),
            )
        if self.schedule == "step":
            if not self.step_size >= 1:
                _fail("step_size must be >= 1", step_size=self.step_size)
            if not 0.0 < self.gamma <= 1.0:
                _fail("gamma must be in (0, 1]", gamma=self.gamma)


@dataclass(frozen=True)
class ModelConfig:
    """MLP shape.  No biases: keeps checkpoints and oracles minimal."""

    in_dim: int = 4
    hidden_dim: int = 8
    out_dim: int = 1
    activation: str = "relu"

    def __post_init__(self) -> None:
        for name in ("in_dim", "hidden_dim", "out_dim"):
            if not getattr(self, name) >= 1:
                _fail(f"{name} must be >= 1", **{name: getattr(self, name)})
        if self.activation not in SUPPORTED_ACTIVATIONS:
            _fail(
                f"unsupported activation '{self.activation}'",
                supported=list(SUPPORTED_ACTIVATIONS),
            )


@dataclass(frozen=True)
class DataConfig:
    """Synthetic regression fixture parameters (fully local, no external data)."""

    n_features: int = 4
    noise_std: float = 0.01
    seed: int = 1234

    def __post_init__(self) -> None:
        if not self.n_features >= 1:
            _fail("n_features must be >= 1", n_features=self.n_features)
        if not self.noise_std >= 0.0:
            _fail("noise_std must be >= 0", noise_std=self.noise_std)


@dataclass(frozen=True)
class RunConfig:
    """Full run configuration, composed from the sub-configs."""

    model: ModelConfig = field(default_factory=ModelConfig)
    precision: PrecisionConfig = field(default_factory=PrecisionConfig)
    scaler: ScalerConfig = field(default_factory=ScalerConfig)
    accumulation: AccumulationConfig = field(default_factory=AccumulationConfig)
    optimizer: OptimizerConfig = field(default_factory=OptimizerConfig)
    data: DataConfig = field(default_factory=DataConfig)
    seed: int = 42
    batch_size: int = 8

    def __post_init__(self) -> None:
        if not self.batch_size >= 1:
            _fail("batch_size must be >= 1", batch_size=self.batch_size)
        if self.model.in_dim != self.data.n_features:
            _fail(
                "model.in_dim must equal data.n_features",
                in_dim=self.model.in_dim,
                n_features=self.data.n_features,
            )
