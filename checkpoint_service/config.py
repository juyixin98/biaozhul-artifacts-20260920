"""Training configuration (immutable, validated at system boundary)."""

from __future__ import annotations

from dataclasses import dataclass

# Checkpoint on-disk format version written by this build.
FORMAT_VERSION = 1

# Tolerances used when comparing an interrupted+resumed run against the
# uninterrupted reference run. Float32 accumulation reorders adds across
# interrupted boundaries (a resumed run takes two save/load round trips per
# interruption), so exact equality is not expected.
PARAM_ATOL = 1e-6
LOSS_ATOL = 1e-6
# Max loss-history prefix length allowed to differ at an interruption.
# Loss entries are only recorded when a checkpoint commit lands, so resumed
# runs are expected to match exactly; the slack covers float ordering.
LOSS_MISMATCH_SLACK = 1


@dataclass(frozen=True)
class TrainConfig:
    """Hyperparameters and data shape for one training run.

    Attributes
    ----------
    n_samples, n_features:
        Shape of the reproducible synthetic dataset.
    data_seed:
        Seed for *data generation*. Kept separate from the training seed so
        resuming a run never depends on RNG side effects from training.
    train_seed:
        Seed for weight initialization and batch shuffling.
    batch_size, lr, momentum, l2:
        Mini-batch SGD with momentum and L2 regularization.
    n_epochs:
        Total number of passes over the dataset.
    checkpoint_every:
        Commit a checkpoint every N optimizer steps.
    """

    n_samples: int = 512
    n_features: int = 8
    data_seed: int = 20260925
    train_seed: int = 42
    batch_size: int = 32
    lr: float = 0.05
    momentum: float = 0.9
    l2: float = 1e-4
    n_epochs: int = 4
    checkpoint_every: int = 8

    def __post_init__(self) -> None:
        if self.n_samples <= 0:
            raise ValueError("n_samples must be positive")
        if self.n_features <= 0:
            raise ValueError("n_features must be positive")
        if self.batch_size <= 0:
            raise ValueError("batch_size must be positive")
        if self.batch_size > self.n_samples:
            raise ValueError("batch_size cannot exceed n_samples")
        if self.lr <= 0:
            raise ValueError("lr must be positive")
        if not 0.0 <= self.momentum < 1.0:
            raise ValueError("momentum must lie in [0, 1)")
        if self.l2 < 0:
            raise ValueError("l2 must be non-negative")
        if self.n_epochs <= 0:
            raise ValueError("n_epochs must be positive")
        if self.checkpoint_every <= 0:
            raise ValueError("checkpoint_every must be positive")

    @property
    def steps_per_epoch(self) -> int:
        """Number of full mini-batches per epoch (final partial batch dropped)."""
        return self.n_samples // self.batch_size

    @property
    def total_steps(self) -> int:
        return self.steps_per_epoch * self.n_epochs

    def to_dict(self) -> dict:
        return {
            "n_samples": self.n_samples,
            "n_features": self.n_features,
            "data_seed": self.data_seed,
            "train_seed": self.train_seed,
            "batch_size": self.batch_size,
            "lr": self.lr,
            "momentum": self.momentum,
            "l2": self.l2,
            "n_epochs": self.n_epochs,
            "checkpoint_every": self.checkpoint_every,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TrainConfig":
        required = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        missing = required - set(data)
        if missing:
            raise ValueError(f"missing config fields: {sorted(missing)}")
        return cls(**{k: data[k] for k in required})
