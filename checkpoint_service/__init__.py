"""Training checkpoint recovery service (pure backend, Python + NumPy)."""

from .config import TrainConfig
from .errors import (
    CheckpointCorruptError,
    CheckpointError,
    CheckpointFormatError,
    CheckpointNotFoundError,
    DatasetMismatchError,
    InvalidRequestError,
    RunExistsError,
    RunNotFoundError,
    UnsupportedCheckpointVersionError,
)
from .model import LinearModel
from .optimizer import MomentumSgd
from .service import TrainingService
from .synthetic import Dataset, make_dataset
from .trainer import Trainer

__all__ = [
    "TrainConfig",
    "TrainingService",
    "Trainer",
    "LinearModel",
    "MomentumSgd",
    "Dataset",
    "make_dataset",
    "CheckpointError",
    "CheckpointNotFoundError",
    "CheckpointCorruptError",
    "CheckpointFormatError",
    "UnsupportedCheckpointVersionError",
    "DatasetMismatchError",
    "InvalidRequestError",
    "RunExistsError",
    "RunNotFoundError",
]
