"""Pydantic schemas for the HTTP layer; mapped onto the frozen core configs."""

from __future__ import annotations

from pydantic import BaseModel, Field


class PrecisionIn(BaseModel):
    lowp_dtype: str = "float16"
    master_dtype: str = "float32"


class ScalerIn(BaseModel):
    init_scale: float = 128.0
    growth_factor: float = 2.0
    backoff_factor: float = 0.5
    growth_interval: int = 2000
    min_scale: float = 1.0


class AccumulationIn(BaseModel):
    micro_batches: int = 1


class OptimizerIn(BaseModel):
    lr: float = 0.01
    momentum: float = 0.9
    weight_decay: float = 0.0
    schedule: str = "constant"
    step_size: int = 10
    gamma: float = 0.5


class ModelIn(BaseModel):
    in_dim: int = 4
    hidden_dim: int = 8
    out_dim: int = 1
    activation: str = "relu"


class DataIn(BaseModel):
    n_features: int = 4
    noise_std: float = 0.01
    seed: int = 1234


class CreateRunIn(BaseModel):
    model: ModelIn = Field(default_factory=ModelIn)
    precision: PrecisionIn = Field(default_factory=PrecisionIn)
    scaler: ScalerIn = Field(default_factory=ScalerIn)
    accumulation: AccumulationIn = Field(default_factory=AccumulationIn)
    optimizer: OptimizerIn = Field(default_factory=OptimizerIn)
    data: DataIn = Field(default_factory=DataIn)
    seed: int = 42
    batch_size: int = 8
    n_samples: int = 256
    amplification: float = 1.0


class AutoWindowsIn(BaseModel):
    n_windows: int = Field(default=1, ge=1, le=10_000)


class CustomWindowIn(BaseModel):
    """Explicit finite batches; length must equal configured micro_batches."""

    batches: list["BatchIn"]


class BatchIn(BaseModel):
    x: list[list[float]]
    y: list[list[float]]


class CheckpointIn(BaseModel):
    directory: str


class LoadCheckpointIn(BaseModel):
    directory: str
    attach_fixture: bool = True


CustomWindowIn.model_rebuild()
