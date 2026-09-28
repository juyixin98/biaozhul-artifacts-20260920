"""Shared pytest fixtures and review logging.

A structured JSONL review log is written for every test session to
``.test-logs/review-<session_id>.jsonl``. Each record is correlatable via:

* ``session_id`` - one pytest invocation
* ``node``        - test node id (input/scenario identity)
* ``run_id``      - engine run id when a run exists
* ``version``     - engine version under test

The reference-equivalence tests append one record per computation step with
the concrete inputs, oracle result, Rete result, diff and explicit verdict.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reteapp.version import __version__  # noqa: E402

LOG_DIR = Path(__file__).resolve().parent.parent / ".test-logs"


class ReviewLog:
    def __init__(self, path: Path, session_id: str) -> None:
        self.path = path
        self.session_id = session_id
        self._fh = path.open("a", encoding="utf-8")

    def record(self, node: str, kind: str, verdict: str, **payload) -> None:
        row = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
            "session_id": self.session_id,
            "node": node,
            "kind": kind,
            "verdict": verdict,
            "version": __version__,
            **payload,
        }
        self._fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True, default=str) + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


def pytest_configure(config: pytest.Config) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    session_id = f"test-{uuid.uuid4().hex[:12]}"
    log_path = LOG_DIR / f"review-{session_id}.jsonl"
    review = ReviewLog(log_path, session_id)
    review.record(
        node="session",
        kind="session_start",
        verdict="INFO",
        python=sys.version.split()[0],
        cwd=os.getcwd(),
    )
    config._review_log = review
    config._review_log_path = log_path


def pytest_unconfigure(config: pytest.Config) -> None:
    review: ReviewLog | None = getattr(config, "_review_log", None)
    path = getattr(config, "_review_log_path", None)
    if review is not None:
        review.record(node="session", kind="session_end", verdict="INFO", log_path=str(path))
        review.close()


@pytest.fixture
def review(request) -> ReviewLog:
    log: ReviewLog = request.config._review_log
    log.record(request.node.nodeid, "test_start", "INFO")
    return log


@pytest.fixture
def session_id(request) -> str:
    return request.config._review_log.session_id


@pytest.fixture
def review_log_path(request) -> Path:
    return request.config._review_log_path


def pytest_terminal_summary(terminalreporter, config) -> None:
    path = getattr(config, "_review_log_path", None)
    if path is not None:
        terminalreporter.write_sep("-", f"structured review log: {path}")
