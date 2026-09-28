"""Structured run logging.

Every plan/run/failure appends one JSON line so an issue can be replayed from
disk: the record carries the run id, request fingerprint, key intermediate
state (retained set, simulated vs measured peak, recompute cost, replay
waves, gradient fingerprints, independent-oracle comparison), the judgement
reason and, on failure, the error category/code.

Log directory: ``$ACT_RECOMPUTE_LOG_DIR`` or ``./logs``.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional


def new_run_id(prefix: str = "run") -> str:
    ts = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    return f"{prefix}-{ts}-{uuid.uuid4().hex[:8]}"


class RunLogger:
    def __init__(self, path: Optional[Path] = None) -> None:
        if path is None:
            path = Path(
                os.environ.get("ACT_RECOMPUTE_LOG_DIR", "logs")
            ) / "runs.jsonl"
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def record(self, record: Dict[str, Any]) -> Dict[str, Any]:
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 **record}
        line = json.dumps(entry, sort_keys=True, default=_default)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        return entry


def _default(obj: Any) -> Any:
    try:
        import numpy as np

        if isinstance(obj, np.ndarray):
            return obj.tolist()
        if isinstance(obj, (np.floating, np.integer)):
            return obj.item()
    except ImportError:  # pragma: no cover
        pass
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    return str(obj)
