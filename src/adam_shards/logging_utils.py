"""Request-id-aware logging adapter."""
from __future__ import annotations

import logging
import sys

_CONFIGURED = False


class RequestLogger(logging.LoggerAdapter):
    """Ensures every log line carries the associated request identity."""

    def process(self, msg, kwargs):
        rid = self.extra.get("request_id") if self.extra else None
        stage = self.extra.get("stage") if self.extra else None
        prefix = f"[request_id={rid or '-'}]"
        if stage:
            prefix += f"[{stage}]"
        return f"{prefix} {msg}", kwargs


def configure_logging(level: int = logging.INFO) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s %(message)s"
    ))
    root = logging.getLogger("adam_shards")
    root.addHandler(handler)
    root.setLevel(level)
    root.propagate = False
    _CONFIGURED = True


def get_logger(request_id: str | None = None, stage: str | None = None) -> RequestLogger:
    configure_logging()
    return RequestLogger(
        logging.getLogger("adam_shards"),
        {"request_id": request_id, "stage": stage},
    )
