"""Logging with request correlation ids and payload redaction.

Numerical payloads (matrix / rhs entries) are treated as potentially sensitive.
They are *never* logged; only shapes and aggregate norms appear in records.
The current request id is carried in a contextvar so every record emitted while
a request is handled can be correlated, including records from deep in the
numerical core.
"""

from __future__ import annotations

import contextvars
import logging
import sys

_REQUEST_ID: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)


def set_request_id(request_id: str) -> contextvars.Token[str]:
    return _REQUEST_ID.set(request_id)


def reset_request_id(token: contextvars.Token[str]) -> None:
    _REQUEST_ID.reset(token)


def get_request_id() -> str:
    return _REQUEST_ID.get()


class _RequestContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _REQUEST_ID.get()
        return True


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger("mipsolver")
    if root.handlers:  # idempotent under uvicorn --reload / repeated tests
        root.setLevel(level)
        return
    root.setLevel(level)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s [%(request_id)s] %(name)s: %(message)s"
        )
    )
    handler.addFilter(_RequestContextFilter())
    root.addHandler(handler)
    root.propagate = False


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"mipsolver.{name}")


def summarize_entries(name: str, n_rows: int, n_cols: int, norm: float | str) -> str:
    """One-line redacted description of a matrix/vector payload."""
    return f"{name}: shape={n_rows}x{n_cols} inf_norm={norm} (entries redacted)"
