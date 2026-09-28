"""运行日志：每次构建/词法运行分配运行编号，记录关键中间状态与判断理由。

日志经 sink 回调落库（app/store/repo.py），可用运行编号重放查询。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable


def new_run_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"run-{ts}-{uuid.uuid4().hex[:8]}"


@dataclass
class LogEntry:
    run_id: str
    seq: int
    ts: str
    module: str
    level: str
    event: str
    state: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""

    def to_row(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "seq": self.seq,
            "ts": self.ts,
            "module": self.module,
            "level": self.level,
            "event": self.event,
            "state_json": json.dumps(self.state, ensure_ascii=False, sort_keys=True),
            "rationale": self.rationale,
        }


Sink = Callable[[LogEntry], None]


class RunLogger:
    """按运行编号顺序记录结构化日志。"""

    def __init__(self, run_id: str, sink: Sink | None = None):
        self.run_id = run_id
        self._sink = sink
        self._seq = 0
        self.entries: list[LogEntry] = []

    def log(
        self,
        module: str,
        event: str,
        state: dict[str, Any] | None = None,
        rationale: str = "",
        level: str = "INFO",
    ) -> LogEntry:
        entry = LogEntry(
            run_id=self.run_id,
            seq=self._seq,
            ts=datetime.now(timezone.utc).isoformat(),
            module=module,
            level=level,
            event=event,
            state=state or {},
            rationale=rationale,
        )
        self._seq += 1
        self.entries.append(entry)
        if self._sink is not None:
            self._sink(entry)
        return entry
