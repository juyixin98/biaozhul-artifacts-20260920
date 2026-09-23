"""Runtime configuration.

Everything is environment-overridable so the same code runs in development,
CI (no DDS) and on a robot. Paths are always resolved and confined to a bag
root so the HTTP layer can never address arbitrary files on disk.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _resolve(p: str | os.PathLike[str]) -> Path:
    return Path(p).expanduser().resolve()


@dataclass(frozen=True)
class Settings:
    # Directory that holds the bags the service is allowed to read.
    bag_root: Path = field(
        default_factory=lambda: _resolve(os.environ.get("BAG_ROOT", "./bags"))
    )
    # Directory where checkpoint JSON files live.
    checkpoint_dir: Path = field(
        default_factory=lambda: _resolve(
            os.environ.get("CHECKPOINT_DIR", "./state/checkpoints")
        )
    )
    # Whether a real rclpy node publishes messages over DDS. Off in CI.
    ros_enabled: bool = field(
        default_factory=lambda: _env_bool("ROS_ENABLED", True)
    )
    # The DDS topic prefix used for playback. Empty => publish on the original
    # topic names.
    topic_prefix: str = field(
        default_factory=lambda: os.environ.get("TOPIC_PREFIX", "/replay")
    )
    # How many published records the in-memory ring sink keeps per session.
    ring_capacity: int = field(
        default_factory=lambda: int(os.environ.get("RING_CAPACITY", "2048"))
    )
    # Reject/allow loop timing tolerances for the engine (seconds).
    min_rate: float = 0.1
    max_rate: float = 10.0

    def ensure_dirs(self) -> None:
        self.bag_root.mkdir(parents=True, exist_ok=True)
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)

    def resolve_bag(self, uri: str) -> Path:
        """Resolve a user supplied bag uri and confine it to the bag root.

        Accepts either a path relative to ``bag_root`` or an absolute path that
        must still live inside ``bag_root``.
        """
        candidate = Path(uri)
        if candidate.is_absolute():
            resolved = candidate.resolve()
        else:
            resolved = (self.bag_root / candidate).resolve()
        try:
            resolved.relative_to(self.bag_root)
        except ValueError as exc:  # pragma: no cover - defensive
            raise PermissionError(
                f"bag uri {uri!r} resolves outside of BAG_ROOT {self.bag_root}"
            ) from exc
        return resolved


def get_settings() -> Settings:
    return Settings()
