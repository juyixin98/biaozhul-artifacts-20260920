"""ASGI entrypoint.

Configuration via environment variables:

* ``STRIPS_DB_PATH``   - SQLite evidence file (default ``data/evidence.db``)
* ``STRIPS_LOG_PATH``  - JSONL replay log (default ``logs/runs.jsonl``)

Run with::

    uvicorn strips_planner.main:app --reload
"""

from __future__ import annotations

import os

from strips_planner.api.app import create_app

DB_PATH = os.environ.get("STRIPS_DB_PATH", "data/evidence.db")
LOG_PATH = os.environ.get("STRIPS_LOG_PATH", "logs/runs.jsonl")

app = create_app(db_path=DB_PATH, log_path=LOG_PATH)
