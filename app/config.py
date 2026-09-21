"""Application configuration loaded from environment variables."""
from __future__ import annotations

import os
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


# Database URL. The docker-compose service provides POSTGRES_* variables;
# default points at a local PostgreSQL for development.
def database_url() -> str:
    explicit = os.environ.get("DATABASE_URL")
    if explicit:
        return explicit
    user = _env("POSTGRES_USER", "nn")
    password = _env("POSTGRES_PASSWORD", "nn")
    host = _env("POSTGRES_HOST", "localhost")
    port = _env("POSTGRES_PORT", "5432")
    db = _env("POSTGRES_DB", "nn_training")
    return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{db}"


# Colon-separated whitelist of directories datasets may be read from.
def data_whitelist() -> list[Path]:
    raw = os.environ.get(
        "DATA_WHITELIST",
        str(Path(__file__).resolve().parent.parent / "data"),
    )
    return [Path(p).resolve() for p in raw.split(os.pathsep) if p.strip()]


# Root directory under which checkpoint files are written.
CHECKPOINT_DIR = Path(
    _env("CHECKPOINT_DIR", str(Path(__file__).resolve().parent.parent / "checkpoints"))
).resolve()

# Queue semantics.
MAX_RUNNING_PER_USER = int(_env("MAX_RUNNING_PER_USER", "3"))
# A claimed job whose heartbeat is older than this is re-queued.
LEASE_SECONDS = int(_env("LEASE_SECONDS", "30"))
# Worker heartbeat interval.
HEARTBEAT_INTERVAL = float(_env("HEARTBEAT_INTERVAL", "5"))
# Number of most-recent checkpoints to keep per job.
MAX_CHECKPOINTS = int(_env("MAX_CHECKPOINTS", "3"))

# Declared tolerance for the resume-consistency guarantee.
RESUME_RTOL = float(_env("RESUME_RTOL", "1e-4"))
RESUME_ATOL = float(_env("RESUME_ATOL", "1e-5"))
