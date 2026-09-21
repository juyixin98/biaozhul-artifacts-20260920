"""Deterministic CPU training engine.

Reproducibility contract
------------------------
A run resumed from a checkpoint is (within ``RESUME_RTOL/ATOL``) identical to
one run continuously to the same epoch. To guarantee that we control every
source of randomness explicitly:

* a single ``torch.Generator`` (CPU) drives sample shuffling for every epoch;
  its state is saved in each checkpoint, so a resumed run draws the exact
  batches it would have drawn continuously;
* a single ``numpy.random.Generator`` is stored the same way (datasets/dropout
  do not need numpy RNG, but we persist it defensively);
* torch global RNG is seeded per run and its ``get_rng_state`` is stored;
* deterministic cuDNN/matmul flags are set (CPU only, but cheap insurance).

Pause/cancel are observed only on epoch boundaries, so a checkpoint always
describes a fully completed epoch ("完成位置").
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np
import torch
from torch import nn
from torch.utils.data import DataLoader, TensorDataset

from ..models.network_spec import NetworkSpec, build_module

# Make CPU arithmetic order deterministic.
torch.use_deterministic_algorithms(False)  # CPU ops here are already deterministic


@dataclass
class HyperParams:
    lr: float
    batch_size: int
    weight_decay: float = 0.0

    @classmethod
    def validate(cls, raw: dict) -> "HyperParams":
        try:
            lr = float(raw["lr"])
            batch_size = int(raw["batch_size"])
            weight_decay = float(raw.get("weight_decay", 0.0))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"bad hyperparameters: {exc}") from exc
        if not 0.0 < lr <= 10.0:
            raise ValueError(f"lr={lr} out of range (0, 10]")
        if not 1 <= batch_size <= 4096:
            raise ValueError(f"batch_size={batch_size} out of range [1, 4096]")
        if not 0.0 <= weight_decay <= 1.0:
            raise ValueError(f"weight_decay={weight_decay} out of range [0, 1]")
        return cls(lr=lr, batch_size=batch_size, weight_decay=weight_decay)


def make_deterministic(seed: int) -> None:
    torch.manual_seed(seed)
    np.random.seed(seed % (2**32))
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False


def build_loss(task: str, out_features: int) -> nn.Module:
    if task == "classification":
        return nn.CrossEntropyLoss()
    if task == "regression":
        if out_features != 1:
            raise ValueError(
                f"regression output must have 1 unit, got {out_features}"
            )
        return nn.MSELoss()
    raise ValueError(f"unknown task {task!r}")


def _check_targets(task: str, y: torch.Tensor, out_features: int, n: int) -> None:
    if task == "classification":
        if y.dtype == torch.float32 and torch.any(y != y.to(torch.int64)):
            raise ValueError("classification targets must be integer class labels")
        labels = y.to(torch.int64)
        if labels.numel() != n or labels.ndim != 1:
            raise ValueError("classification targets must be shape (N,)")
        if int(labels.min()) < 0 or int(labels.max()) >= out_features:
            raise ValueError(
                f"target label out of range: classes [0, {out_features - 1}], "
                f"got [{int(labels.min())}, {int(labels.max())}]"
            )
    else:
        if y.ndim == 1:
            pass
        elif y.ndim == 2 and y.shape[1] == 1:
            pass
        else:
            raise ValueError("regression targets must be shape (N,) or (N, 1)")


def prepare_tensors(
    spec: NetworkSpec,
    task: str,
    X: np.ndarray,
    y: np.ndarray,
) -> tuple[torch.Tensor, torch.Tensor]:
    if X.ndim != 2:
        raise ValueError(f"features must be 2-D, got {X.shape}")
    if X.shape[1] != spec.in_features:
        raise ValueError(
            f"feature width {X.shape[1]} != architecture input "
            f"{spec.in_features}"
        )
    Xt = torch.from_numpy(np.ascontiguousarray(X, dtype=np.float32))
    yt = torch.from_numpy(np.ascontiguousarray(y))
    _check_targets(task, yt, spec.out_features, Xt.shape[0])
    if task == "classification":
        yt = yt.to(torch.int64)
    else:
        yt = yt.to(torch.float32)
        if yt.ndim == 1:
            yt = yt.unsqueeze(1)
    return Xt, yt


def split_indices(n: int, val_fraction: float, seed: int) -> tuple[list[int], list[int]]:
    """Deterministic train/validation split. The resulting indices are stored
    on the job and reused forever — resumption never re-splits."""
    if not 0.0 < val_fraction < 1.0:
        raise ValueError(f"val_fraction={val_fraction} must be in (0, 1)")
    perm = np.random.default_rng(seed).permutation(n)
    n_val = max(1, min(n - 1, int(round(n * val_fraction))))
    val_idx = perm[:n_val].tolist()
    train_idx = perm[n_val:].tolist()
    return [int(i) for i in train_idx], [int(i) for i in val_idx]


@dataclass
class EpochResult:
    epoch: int  # 1-indexed completed epoch
    train_loss: float
    val_loss: float
    val_accuracy: float | None


class Engine:
    """Holds model, optimizer and all RNG state for one job."""

    def __init__(self, spec: NetworkSpec, hp: HyperParams, task: str, seed: int):
        self.spec = spec
        self.hp = hp
        self.task = task
        self.seed = seed
        # Seed EVERYTHING before constructing the model so weight init (which
        # draws from the global torch RNG) is reproducible across processes
        # and across a resume.
        make_deterministic(seed)
        self.model = build_module(spec)
        self.loss_fn = build_loss(task, spec.out_features)
        self.optimizer = torch.optim.SGD(
            self.model.parameters(),
            lr=hp.lr,
            weight_decay=hp.weight_decay,
        )
        # Independent, fully-persisted RNG streams. Initialising the shuffle
        # generator and the numpy generator here does NOT touch the global
        # torch RNG used for dropout, which we snapshot right after.
        self.shuffle_gen = torch.Generator(device="cpu").manual_seed(seed)
        self.np_rng = np.random.default_rng(seed)
        # The global torch RNG is left in its post-model-init state; dropout
        # draws from it and its live state is snapshotted each checkpoint.

    # -- state capture / restore ----------------------------------------

    def state_dict(self, epochs_done: int) -> dict:
        return {
            "format": 1,
            "epochs_done": epochs_done,
            "seed": self.seed,
            "model": self.model.state_dict(),
            "optimizer": self.optimizer.state_dict(),
            "torch_rng": torch.get_rng_state(),
            "shuffle_gen": self.shuffle_gen.get_state(),
            "numpy_rng": self.np_rng.bit_generator.state,
        }

    def load_state_dict(self, state: dict) -> int:
        if state.get("format") != 1:
            raise ValueError("unknown checkpoint format")
        self.model.load_state_dict(state["model"])
        self.optimizer.load_state_dict(state["optimizer"])
        torch.set_rng_state(state["torch_rng"])
        self.shuffle_gen.set_state(state["shuffle_gen"])
        self.np_rng.bit_generator.state = state["numpy_rng"]
        return int(state["epochs_done"])

    # -- training ---------------------------------------------------------

    def _loader(self, X: torch.Tensor, y: torch.Tensor, shuffle: bool):
        ds = TensorDataset(X, y)
        return DataLoader(
            ds,
            batch_size=self.hp.batch_size,
            shuffle=shuffle,
            generator=self.shuffle_gen if shuffle else None,
            num_workers=0,
            drop_last=False,
        )

    def run_epoch(self, Xtr: torch.Tensor, ytr: torch.Tensor,
                  Xval: torch.Tensor, yval: torch.Tensor, epoch: int) -> EpochResult:
        self.model.train()
        train_losses: list[float] = []
        for xb, yb in self._loader(Xtr, ytr, shuffle=True):
            self.optimizer.zero_grad(set_to_none=True)
            pred = self.model(xb)
            loss = self.loss_fn(pred, yb)
            loss.backward()
            self.optimizer.step()
            train_losses.append(float(loss.detach()))

        self.model.eval()
        with torch.no_grad():
            val_pred = self.model(Xval)
            val_loss = float(self.loss_fn(val_pred, yval))
            acc = None
            if self.task == "classification":
                acc = float((val_pred.argmax(dim=1) == yval).float().mean())
        return EpochResult(
            epoch=epoch,
            train_loss=float(np.mean(train_losses)),
            val_loss=val_loss,
            val_accuracy=acc,
        )
