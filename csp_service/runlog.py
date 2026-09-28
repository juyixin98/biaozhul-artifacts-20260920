"""Structured run logging for tests and verification scripts.

Each record is one JSON line keyed by run_id so a log file can be traced
back to the exact input (fixture name + problem hash) and the tool versions
that produced it. Records carry a `step` field showing progress and a
`basis` field stating the decision ground for every verdict.
"""
from __future__ import annotations

import hashlib
import json
import platform
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import csp_service


def tool_versions() -> dict[str, str]:
    versions = {
        "python": platform.python_version(),
        "csp_service": csp_service.__version__,
    }
    for pkg in ("fastapi", "pydantic", "pytest", "uvicorn", "httpx"):
        try:
            from importlib.metadata import version

            versions[pkg] = version(pkg)
        except Exception:
            pass
    return versions


def problem_fingerprint(problem: dict) -> str:
    blob = json.dumps(problem, sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


class RunLogger:
    def __init__(self, log_dir: str | Path, run_id: str | None = None):
        self.run_id = run_id or (
            "verify-"
            + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            + "-"
            + uuid.uuid4().hex[:8]
        )
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.log_dir / f"{self.run_id}.jsonl"
        self._step = 0
        self.log("run_start", versions=tool_versions())

    def log(self, event: str, **fields) -> None:
        self._step += 1
        record = {
            "run_id": self.run_id,
            "step": self._step,
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **fields,
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
