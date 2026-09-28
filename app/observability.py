"""Run-scoped structured logging.

Every mining run and every request gets a ``run_id`` that is attached to every
log record it produces, so a test log can be tied back to a concrete input or
run identity.  Logs are emitted both to stderr and to a per-run JSONL file under
the configured log directory.
"""
from __future__ import annotations

import json
import logging
import sys
import uuid
from pathlib import Path
from typing import Any

from app.config import settings

_LOG_FORMAT = "%(asctime)s | %(levelname)-7s | run=%(run_id)s | %(message)s"


class _RunIdFilter(logging.Filter):
    def __init__(self, run_id: str) -> None:
        super().__init__()
        self.run_id = run_id

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "run_id"):
            record.run_id = self.run_id
        return True


class RunLogger:
    """Small structured logger wrapper bound to one run identity."""

    def __init__(self, logger: logging.Logger, run_id: str, fh: logging.Handler | None):
        self._logger = logger
        self.run_id = run_id
        self._fh = fh

    def _emit(self, level: int, event: str, fields: dict[str, Any]) -> None:
        payload = {"event": event, **fields}
        rendered = json.dumps(payload, ensure_ascii=False, default=str, sort_keys=True)
        self._logger.log(level, "%s %s", event, rendered)

    def info(self, event: str, **fields: Any) -> None:
        self._emit(logging.INFO, event, fields)

    def warning(self, event: str, **fields: Any) -> None:
        self._emit(logging.WARNING, event, fields)

    def error(self, event: str, **fields: Any) -> None:
        self._emit(logging.ERROR, event, fields)

    def step(self, event: str, **fields: Any) -> None:
        """Progress / computation-step record; ``event`` IS the event name."""
        self._emit(logging.INFO, event, fields)

    def decision(self, event: str, **fields: Any) -> None:
        """Record a judgement; ``event`` names it and fields carry the basis."""
        self._emit(logging.INFO, event, fields)

    def close(self) -> None:
        if self._fh is not None:
            self._logger.removeHandler(self._fh)
            self._fh.close()


def new_run_id(prefix: str = "run") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def get_run_logger(run_id: str | None = None, *, name: str = "spm",
                   prefix: str = "run", file_logging: bool = True) -> RunLogger:
    run_id = run_id or new_run_id(prefix)
    logger = logging.getLogger(f"spm.{name}.{run_id}")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.handlers.clear()

    run_filter = _RunIdFilter(run_id)

    stream = logging.StreamHandler(sys.stderr)
    stream.setLevel(logging.INFO)
    stream.setFormatter(logging.Formatter(_LOG_FORMAT))
    stream.addFilter(run_filter)
    logger.addHandler(stream)

    fh: logging.FileHandler | None = None
    if file_logging:
        log_dir = Path(settings.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_dir / f"{run_id}.jsonl", encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            '{"ts":"%(asctime)s","level":"%(levelname)s","run_id":"%(run_id)s",'
            '"msg":%(message_json)s}',
        ))

        class _JsonCtx(logging.Filter):
            def filter(self, record: logging.LogRecord) -> bool:
                # RunLogger always emits "<event> {valid-json}"; the first
                # space separates the event name from the JSON body.
                event, _, body = record.getMessage().partition(" ")
                parsed = json.loads(body)
                record.message_json = json.dumps(
                    {"event": event, **parsed}, ensure_ascii=False, default=str)
                return True

        fh.addFilter(run_filter)
        fh.addFilter(_JsonCtx())
        logger.addHandler(fh)

    return RunLogger(logger, run_id, fh)
