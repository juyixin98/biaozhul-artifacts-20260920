"""Runtime configuration, overridable through environment variables."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, replace

from .archive_guard.limits import Limits


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value


@dataclass(frozen=True)
class Settings:
    #: Directory where successfully extracted archives are published.
    store_dir: str
    #: Directory for in-flight upload spool files.
    spool_dir: str
    limits: Limits

    @classmethod
    def from_env(cls) -> "Settings":
        base = os.environ.get(
            "ARCHIVE_GUARD_HOME",
            os.path.join(os.getcwd(), ".archive_guard_data"),
        )
        store_dir = os.environ.get("ARCHIVE_GUARD_STORE",
                                  os.path.join(base, "extractions"))
        spool_dir = os.environ.get("ARCHIVE_GUARD_SPOOL",
                                  os.path.join(base, "spool"))
        limits = Limits(
            max_upload_bytes=_env_int("AG_MAX_UPLOAD_BYTES",
                                      Limits.max_upload_bytes),
            max_entries=_env_int("AG_MAX_ENTRIES", Limits.max_entries),
            max_total_size=_env_int("AG_MAX_TOTAL_SIZE",
                                    Limits.max_total_size),
            max_total_written_bytes=_env_int(
                "AG_MAX_TOTAL_WRITTEN_BYTES", Limits.max_total_written_bytes
            ),
            max_single_file_bytes=_env_int("AG_MAX_SINGLE_FILE_BYTES",
                                           Limits.max_single_file_bytes),
            max_path_depth=_env_int("AG_MAX_PATH_DEPTH",
                                    Limits.max_path_depth),
            max_symlink_hops=_env_int("AG_MAX_SYMLINK_HOPS",
                                      Limits.max_symlink_hops),
        )
        return cls(store_dir=store_dir, spool_dir=spool_dir, limits=limits)

    def with_limits(self, **overrides) -> "Settings":
        return replace(self, limits=replace(self.limits, **overrides))


def get_settings() -> Settings:
    settings = getattr(_STATE, "settings", None)
    if settings is None:
        settings = Settings.from_env()
        os.makedirs(settings.store_dir, mode=0o700, exist_ok=True)
        os.makedirs(settings.spool_dir, mode=0o700, exist_ok=True)
        _STATE.settings = settings
    return settings


def set_settings(settings: Settings) -> None:
    """Test hook: replace the process-wide settings."""
    os.makedirs(settings.store_dir, mode=0o700, exist_ok=True)
    os.makedirs(settings.spool_dir, mode=0o700, exist_ok=True)
    _STATE.settings = settings


class _STATE:
    settings: Settings | None = None


def new_spool_file(spool_dir: str | None = None):
    """Create a closed named temporary file used to receive an upload."""
    directory = spool_dir or get_settings().spool_dir
    os.makedirs(directory, mode=0o700, exist_ok=True)
    return tempfile.NamedTemporaryFile(
        prefix="upload-", suffix=".tar", dir=directory, delete=False
    )
