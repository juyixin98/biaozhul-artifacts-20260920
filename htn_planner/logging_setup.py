"""Structured logging setup shared by the API and CLI.

Every formatter includes ``request_id``; a context variable supplies it while
a request is handled, defaulting to ``-`` otherwise.
"""

from __future__ import annotations

import contextvars
import logging

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)

_FORMAT = (
    "%(asctime)s %(levelname)s [%(name)s] request_id=%(request_id)s %(message)s"
)


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get("-")
        return True


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(_FORMAT))
    handler.addFilter(_RequestIdFilter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper() if isinstance(level, str) else level)
