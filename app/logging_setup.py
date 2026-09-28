"""Structured logging setup.

Every log line is key=value formatted and includes the request id when one
is bound via a :class:`logging.LoggerAdapter` (the service layer does this).
A single formatter is attached to stderr and, optionally, a log file.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path


class KeyValueFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        parts = [
            self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            record.levelname.lower(),
            record.name,
        ]
        rid = getattr(record, "request_id", None)
        if rid:
            parts.append(f"request_id={rid}")
        message = record.getMessage()
        parts.append(message)
        if record.exc_info:
            parts.append(self.formatException(record.exc_info))
        return " | ".join(parts)


_configured = False


def setup_logging(level: str = "INFO", log_file: str | None = None) -> None:
    global _configured
    root = logging.getLogger()
    if _configured:
        return
    root.setLevel(level.upper())
    formatter = KeyValueFormatter()

    err = logging.StreamHandler(sys.stderr)
    err.setFormatter(formatter)
    root.addHandler(err)

    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(formatter)
        root.addHandler(fh)

    _configured = True
