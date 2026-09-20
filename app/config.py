"""Process-wide configuration, sourced from environment variables / .env."""
from __future__ import annotations

import os
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = (
        "postgresql+psycopg2://trainer:trainer@localhost:5432/nn_training"
    )
    # Colon-separated list of directories datasets may be read from.
    data_whitelist_dirs: str = "/tmp/nnlab-data"
    checkpoint_dir: str = "/tmp/nnlab-checkpoints"

    # Leader lease (seconds) granted to the executor that claims a job.
    leader_lease_seconds: int = 30
    # How often a running executor renews its lease.
    worker_heartbeat_interval: int = 5
    # Number of worker threads inside the single API/worker process.
    worker_threads: int = 2
    worker_poll_interval: float = 2.0
    max_running_per_user: int = 3

    worker_enabled: bool = True

    @property
    def whitelist(self) -> list[str]:
        out = []
        for p in self.data_whitelist_dirs.split(os.pathsep):
            p = p.strip()
            if p:
                out.append(os.path.realpath(p))
        return out


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
