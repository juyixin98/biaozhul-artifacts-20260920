"""Configuration loading and deterministic synthetic fixtures.

No external services or real business data: the dataset is generated locally
from a fixed-seed "teacher" network plus bounded noise, and a generated sample
file is shipped under ``data/`` for first-time users.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

import numpy as np

from .adam import AdamConfig
from .graph import GraphSpec
from .tensor_types import resolve_dtype


@dataclass(frozen=True)
class AppConfig:
    storage_root: str
    seed: int
    dtype: np.dtype
    spec: GraphSpec
    n_samples: int
    adam: AdamConfig
    train_steps: int
    api_host: str
    api_port: int

    @classmethod
    def load(cls, path: str | None = None) -> "AppConfig":
        if path is None:
            path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "config", "default.json")
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        return cls(
            storage_root=str(raw["storage_root"]),
            seed=int(raw["seed"]),
            dtype=resolve_dtype(str(raw.get("dtype", "float64"))),
            spec=GraphSpec.from_config(raw["model"]),
            n_samples=int(raw.get("data", {}).get("n_samples", 12)),
            adam=AdamConfig.from_config(raw.get("adam")),
            train_steps=int(raw.get("train_steps", 6)),
            api_host=str(raw.get("api", {}).get("host", "127.0.0.1")),
            api_port=int(raw.get("api", {}).get("port", 8000)),
        )


def make_dataset(n_samples: int, in_dim: int, spec: GraphSpec, seed: int) -> dict[str, np.ndarray]:
    """Produce (x, y) from a fixed teacher MLP plus deterministic noise."""

    rng = np.random.default_rng(seed + 7919)
    x = rng.uniform(-1.5, 1.5, size=(n_samples, in_dim))
    # Teacher shares the architecture but not the student's draw.
    from .graph import MLPModule

    teacher = MLPModule(spec, dtype=np.float64, seed=seed + 104729)
    y = teacher.forward(x) + 0.01 * rng.standard_normal((n_samples, spec.dims[-1]))
    return {"x": np.ascontiguousarray(x), "y": np.ascontiguousarray(y, dtype=np.float64)}


def sample_data_path(root: str | None = None) -> str:
    base = root or os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    return os.path.join(base, "data", "sample.npz")


def write_sample_dataset(cfg: AppConfig, path: str | None = None) -> str:
    path = path or sample_data_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = make_dataset(cfg.n_samples, cfg.spec.dims[0], cfg.spec, cfg.seed)
    tmp = path + ".tmp.npz"
    np.savez(tmp, x=data["x"], y=data["y"], seed=cfg.seed)
    os.replace(tmp, path)
    return path


def load_sample_dataset(cfg: AppConfig, path: str | None = None) -> dict[str, np.ndarray]:
    path = path or sample_data_path()
    if os.path.isfile(path):
        with np.load(path) as zf:
            x, y = zf["x"].copy(), zf["y"].copy()
        if x.shape == (cfg.n_samples, cfg.spec.dims[0]) and y.shape == (cfg.n_samples, cfg.spec.dims[-1]):
            return {"x": x.astype(cfg.dtype), "y": y.astype(cfg.dtype)}
    # Regenerate deterministically if the shipped file is absent or incompatible.
    data = make_dataset(cfg.n_samples, cfg.spec.dims[0], cfg.spec, cfg.seed)
    return {"x": data["x"].astype(cfg.dtype), "y": data["y"].astype(cfg.dtype)}
