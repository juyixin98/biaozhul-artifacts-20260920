"""Logging setup: every record carries the request identity.

The request id is kept in a contextvar so log lines emitted anywhere inside
a request — corpus validation, index build, query mining — are correlated
without threading the id through every signature.
"""

from __future__ import annotations

import contextvars
import logging
import uuid

from app.config import APP_VERSION

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)


def new_request_id() -> str:
    return uuid.uuid4().hex[:12]


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        record.app_version = APP_VERSION
        return True


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s level=%(levelname)s request_id=%(request_id)s "
            "version=%(app_version)s %(name)s: %(message)s"
        )
    )
    # The filter must sit on the handler: logger-level filters do not apply
    # to records propagated from child loggers.
    handler.addFilter(_ContextFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
