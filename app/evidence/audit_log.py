"""审计日志文件输出（JSON Lines）。

数据库的 ``audit_events`` 是结构化证据；本模块额外把同样的事件以
JSONL 追加到本地日志文件，满足"日志可解释、关联请求身份"的验收要求：
每行含 request_id、角色、动作、结果、契约版本与失败类别。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any


class AuditFileLogger:
    def __init__(self, path: str | None):
        self._path = Path(path) if path else None
        self._lock = threading.Lock()
        if self._path:
            self._path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, event: dict[str, Any]) -> None:
        if self._path is None:
            return
        line = json.dumps(event, ensure_ascii=False, sort_keys=True)
        with self._lock:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    @property
    def path(self) -> str | None:
        return str(self._path) if self._path else None
