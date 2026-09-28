"""Logging configuration: every line carries request identity and version."""
from __future__ import annotations

import logging
import sys

from . import ENGINE_VERSION


class _RequestContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.engine_version = ENGINE_VERSION
        return True


def configure_logging(level: str = "INFO", log_path: str | None = None) -> None:
    formatter = logging.Formatter(
        fmt=(
            "%(asctime)s %(levelname)s [%(engine_version)s] "
            "%(name)s :: %(message)s"
        ),
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    stream.addFilter(_RequestContextFilter())
    root.addHandler(stream)

    if log_path:
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        file_handler.addFilter(_RequestContextFilter())
        root.addHandler(file_handler)
