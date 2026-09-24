"""Configuration for the replay service.

Everything can be overridden with environment variables so the service needs
no generated settings file to run.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _split_paths(raw: str) -> list[Path]:
    out: list[Path] = []
    for part in raw.split(os.pathsep):
        part = part.strip()
        if part:
            out.append(Path(part).expanduser().resolve())
    return out


@dataclass(frozen=True)
class Settings:
    # Dirs under which a bag URI supplied by a client is allowed to live.
    # Default: the bundled examples dir + current working directory.
    bag_roots: list[Path] = field(
        default_factory=lambda: _split_paths(
            os.environ.get(
                "REPLAY_BAG_ROOTS",
                str(Path(__file__).resolve().parent.parent / "examples")
                + os.pathsep
                + str(Path.cwd()),
            )
        )
    )
    # Where checkpoint JSON files are stored.
    state_dir: Path = field(
        default_factory=lambda: Path(
            os.environ.get("REPLAY_STATE_DIR", Path.cwd() / ".replay_state")
        ).expanduser().resolve()
    )
    # HMAC secret for checkpoint signatures. If the env var is unset a random
    # key is generated once per process and persisted inside state_dir.
    secret_file: Path = field(
        default_factory=lambda: Path(
            os.environ.get(
                "REPLAY_SECRET_FILE",
                Path(os.environ.get("REPLAY_STATE_DIR", Path.cwd() / ".replay_state"))
                .expanduser().resolve()
                / "hmac.key",
            )
        )
    )
    host: str = field(default_factory=lambda: os.environ.get("REPLAY_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(os.environ.get("REPLAY_PORT", "8000")))
    # Scheduler granularity; small enough that rate 100x stays responsive.
    tick_seconds: float = field(
        default_factory=lambda: float(os.environ.get("REPLAY_TICK_SECONDS", "0.005"))
    )
    # Publish transport when REPLAY_TRANSPORT=ros: needs a sourced ROS env.
    transport: str = field(
        default_factory=lambda: os.environ.get("REPLAY_TRANSPORT", "loopback")
    )
    # DDS topic on which control envelopes are announced (ros transport).
    control_topic: str = field(
        default_factory=lambda: os.environ.get("REPLAY_CONTROL_TOPIC", "/replay/control")
    )


settings = Settings()
