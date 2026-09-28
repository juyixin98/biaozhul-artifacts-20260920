"""结构化运行日志。

每条日志是一行 JSON，字段可关联到具体输入与运行身份：
``run_id``（唯一）、``timestamp``、``service_version``、``corpus``、
``input``、计算步骤/进度、候选代价、最终判定（``verdict``）。

异常与未知状态显式记为 ``verdict="error:<category>"``，绝不与成功混淆；
另写人类可读的文本日志便于直接复核。
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from wfst import __version__


def new_run_id() -> str:
    return f"run-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"


class RunLogger:
    """同时写 JSONL（机器可读）与文本日志（人工复核）。"""

    def __init__(self, log_dir: str | Path):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = self.log_dir / "runs.jsonl"
        self.text_path = self.log_dir / "runs.log"
        self._text = logging.getLogger("wfst.runs")
        if not self._text.handlers:
            handler = logging.FileHandler(self.text_path, encoding="utf-8")
            handler.setFormatter(
                logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S")
            )
            self._text.addHandler(handler)
            self._text.setLevel(logging.INFO)

    def event(self, run_id: str, kind: str, **fields: Any) -> dict:
        record = {
            "run_id": run_id,
            "ts": datetime.now(timezone.utc).isoformat(),
            "service_version": __version__,
            "event": kind,
            **fields,
        }
        with self.jsonl_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        summary = _summarize(record)
        self._text.info("[%s] %s %s", run_id, kind, summary)
        return record


def _summarize(record: dict) -> str:
    parts = []
    for key in ("corpus", "input", "mode", "verdict", "category", "complete",
                "pops", "budget", "outputs"):
        if key in record:
            parts.append(f"{key}={record[key]!r}")
    return " ".join(parts)
