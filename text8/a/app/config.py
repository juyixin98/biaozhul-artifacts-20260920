import os
from dataclasses import dataclass


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    database_url: str = _env(
        "DATABASE_URL", "postgresql+psycopg2://flow:flow@localhost:5432/flow"
    )
    worker_enabled: bool = _env("WORKER_ENABLED", "true").lower() in ("1", "true", "yes")
    worker_poll_seconds: float = float(_env("WORKER_POLL_SECONDS", "2"))
    worker_batch_size: int = int(_env("WORKER_BATCH_SIZE", "20"))


settings = Settings()
