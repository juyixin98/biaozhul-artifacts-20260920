"""Structured logging configuration.

Log lines always carry the request id, rank (where applicable), stage and
storage location, so an operator can correlate API calls with worker and
checkpoint activity.
"""

from __future__ import annotations

import logging

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"


def configure_logging(level: int | str = logging.INFO) -> None:
    root = logging.getLogger("adam_shard")
    if not any(getattr(h, "_adam_shard_handler", False) for h in root.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(_FORMAT))
        handler._adam_shard_handler = True  # type: ignore[attr-defined]
        root.addHandler(handler)
    root.setLevel(level)
    root.propagate = False
