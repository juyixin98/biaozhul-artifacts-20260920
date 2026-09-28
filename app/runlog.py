"""Structured run logging.

Every request gets a run id (``run_<hex>``). Each pipeline stage appends an
entry with the key intermediate state and the rationale for decisions, so a
reported problem can be replayed from its run id via ``GET /api/runs/{id}``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from .index.db import Database
from .index.models import RunLogEntry


def new_run_id() -> str:
    return "run_" + uuid.uuid4().hex[:16]


class RunLogger:
    def __init__(self, db: Database, run_id: str, op: str) -> None:
        self._db = db
        self.run_id = run_id
        self.op = op

    def log(self, stage: str, **detail: Any) -> None:
        self._db.add_log(
            RunLogEntry(
                run_id=self.run_id,
                ts=datetime.now(timezone.utc).isoformat(),
                op=self.op,
                stage=stage,
                detail=detail,
            )
        )
