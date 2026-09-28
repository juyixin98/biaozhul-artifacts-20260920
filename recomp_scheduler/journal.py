"""运行日志：可重放的运行编号、关键中间状态与判断理由。

每次计划/执行/验证都由 :class:`Journal` 记录为一条 JSONL，包含：

- ``run_id``   全局唯一编号（UTC 时间戳 + 进程内自增序号 + 随机后缀），
               凭此编号可在同一目录中精确定位并复现一次运行；
- ``kind``     plan / execute / verify / error；
- 关键中间状态：方案边界、块内容、静态画像、运行时峰值、对账结论、
               梯度范数、副作用发放/重放计数、RNG 快照账；
- ``reason``   人类可读的判断理由（含失败类别）。

错误同样记录（:meth:`Journal.record_error`），输入错误/状态冲突/资源耗尽/
计算失败四类以 ``category`` 区分。日志文件按启动日切分，全部为本地文件，
不涉及任何外部服务。
"""

from __future__ import annotations

import itertools
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_counter = itertools.count(1)
_counter_lock = threading.Lock()


def new_run_id(prefix: str = "run") -> str:
    """生成形如 ``run-20260928T113045Z-0001-a1b2c3d4`` 的运行编号。"""
    with _counter_lock:
        seq = next(_counter)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}-{ts}-{seq:04d}-{uuid.uuid4().hex[:8]}"


def _json_default(obj: Any) -> Any:
    if hasattr(obj, "tolist"):
        return obj.tolist()
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    if isinstance(obj, Path):
        return str(obj)
    return repr(obj)


class Journal:
    """JSONL 运行日志（线程安全）。"""

    def __init__(self, directory: str | os.PathLike[str] = "logs") -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _path_for_today(self) -> Path:
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        return self.directory / f"runs-{day}.jsonl"

    def write(self, record: dict[str, Any]) -> str:
        record = dict(record)
        run_id = record.get("run_id") or new_run_id(record.get("kind", "run"))
        record["run_id"] = run_id
        record.setdefault("recorded_at", datetime.now(timezone.utc).isoformat())
        line = json.dumps(record, default=_json_default, ensure_ascii=False, sort_keys=True)
        with self._lock:
            with self._path_for_today().open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        return run_id

    def record_plan(
        self,
        *,
        graph_fingerprint: str,
        budget: int,
        plan_dict: dict[str, Any],
        profile: dict[str, Any],
        candidates_evaluated: int,
        reason: str,
    ) -> str:
        return self.write(
            {
                "kind": "plan",
                "graph_fingerprint": graph_fingerprint,
                "budget_elements": budget,
                "plan": plan_dict,
                "profile": profile,
                "candidates_evaluated": candidates_evaluated,
                "reason": reason,
            }
        )

    def record_execute(
        self,
        *,
        graph_fingerprint: str,
        run_id: str | None = None,
        seed: int,
        plan_dict: dict[str, Any],
        predicted_peak: int,
        runtime_peak: int,
        peak_match: bool,
        effects_emitted: int,
        effects_replayed: int,
        grad_norms: dict[str, float],
        snapshot_balance: dict[str, int],
        reason: str,
    ) -> str:
        return self.write(
            {
                "kind": "execute",
                "run_id": run_id or new_run_id("exec"),
                "graph_fingerprint": graph_fingerprint,
                "seed": seed,
                "plan": plan_dict,
                "predicted_peak": predicted_peak,
                "runtime_peak": runtime_peak,
                "peak_match": peak_match,
                "effects": {
                    "emitted": effects_emitted,
                    "replayed": effects_replayed,
                },
                "snapshot_balance": snapshot_balance,
                "grad_norms": grad_norms,
                "reason": reason,
            }
        )

    def record_verify(self, **fields: Any) -> str:
        fields["kind"] = "verify"
        return self.write(fields)

    def record_error(self, error: Exception, *, context: dict[str, Any] | None = None) -> str:
        from .errors import RecompError

        if isinstance(error, RecompError):
            payload = error.to_dict()
        else:
            payload = {
                "category": "compute_failure",
                "message": f"{type(error).__name__}: {error}",
                "details": {},
            }
        return self.write(
            {
                "kind": "error",
                "error": payload,
                "context": context or {},
                "reason": payload["message"],
            }
        )

    def load(self, run_id: str) -> dict[str, Any]:
        """按 run_id 取回一条记录（用于复现/排障）。"""
        needle = run_id
        for path in sorted(self.directory.glob("runs-*.jsonl"), reverse=True):
            with path.open(encoding="utf-8") as fh:
                for line in fh:
                    rec = json.loads(line)
                    if rec.get("run_id") == needle:
                        return rec
        raise KeyError(run_id)

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for path in sorted(self.directory.glob("runs-*.jsonl"), reverse=True):
            with path.open(encoding="utf-8") as fh:
                records.extend(json.loads(line) for line in fh if line.strip())
        return records[-limit:]
