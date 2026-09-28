"""Configuration loading and validation."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .adam import AdamConfig


@dataclass(frozen=True)
class AppConfig:
    checkpoint_dir: str
    fixture_path: str
    seed: int
    layer_dims: tuple[int, ...]
    batch_size: int
    train_steps: int
    adam: AdamConfig
    save_world_size: int
    restore_world_size: int
    tight_tol: float
    loose_tol: float

    @classmethod
    def load(cls, path: str | Path) -> "AppConfig":
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        required = {"checkpoint_dir", "fixture_path", "seed", "layer_dims",
                    "batch_size", "train_steps", "lr", "beta1", "beta2", "eps",
                    "save_world_size", "restore_world_size",
                    "tight_tol", "loose_tol"}
        missing = required - set(raw)
        if missing:
            raise ValueError(f"config missing keys: {sorted(missing)}")
        dims = tuple(int(d) for d in raw["layer_dims"])
        return cls(
            checkpoint_dir=str(raw["checkpoint_dir"]),
            fixture_path=str(raw["fixture_path"]),
            seed=int(raw["seed"]),
            layer_dims=dims,
            batch_size=int(raw["batch_size"]),
            train_steps=int(raw["train_steps"]),
            adam=AdamConfig(
                lr=float(raw["lr"]), beta1=float(raw["beta1"]),
                beta2=float(raw["beta2"]), eps=float(raw["eps"]),
            ),
            save_world_size=int(raw["save_world_size"]),
            restore_world_size=int(raw["restore_world_size"]),
            tight_tol=float(raw["tight_tol"]),
            loose_tol=float(raw["loose_tol"]),
        )
