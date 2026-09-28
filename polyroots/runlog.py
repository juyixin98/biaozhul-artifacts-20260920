"""可重放运行日志。

每次求解写一条 JSON 记录到 <log_dir>/<YYYYMMDD>/<run_id>.json，记录：
run_id、时间、规范化输入、选项、内核关键中间状态、逐根判断理由、最终状态。
另在 run_registry.json 记录 run_id -> 输入指纹，用于检测同一 run_id
携带不同输入的状态冲突（幂等键冲突 → state_conflict）。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import ConflictingStateError


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def input_fingerprint(coeffs_asc: list[list[float]], options: dict[str, Any]) -> str:
    payload = json.dumps(
        {"coeffs": coeffs_asc, "options": options}, sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class RunStore:
    """文件型运行记录与幂等注册表（线程安全，适合单实例本地服务）。"""

    def __init__(self, log_dir: str):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._registry_path = self.log_dir / "run_registry.json"
        self._registry: dict[str, dict[str, str]] = self._load_registry()

    def _load_registry(self) -> dict[str, dict[str, str]]:
        if self._registry_path.exists():
            with self._registry_path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        return {}

    def _save_registry_locked(self) -> None:
        tmp = self._registry_path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(self._registry, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self._registry_path)

    def register_or_conflict(self, run_id: str, fingerprint: str) -> None:
        """同一 run_id 允许完全相同输入（幂等重放）；不同输入判为状态冲突。"""
        with self._lock:
            existing = self._registry.get(run_id)
            if existing is not None and existing["fingerprint"] != fingerprint:
                raise ConflictingStateError(
                    f"run_id={run_id} 已关联不同输入，拒绝覆盖（状态冲突）",
                    {"run_id": run_id,
                     "existing_fingerprint": existing["fingerprint"],
                     "new_fingerprint": fingerprint},
                )
            if existing is None:
                self._registry[run_id] = {"fingerprint": fingerprint,
                                          "created_at": utc_now_iso()}
                self._save_registry_locked()

    def write_run(self, record: dict[str, Any]) -> Path:
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        out_dir = self.log_dir / day
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{record['run_id']}.json"
        with self._lock:
            tmp = path.with_suffix(".json.tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(record, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        return path

    def load_run(self, run_id: str) -> dict[str, Any] | None:
        # 先在当天找，再回扫其它日期目录（运行跨天也能重放）
        for candidate in sorted(self.log_dir.glob(f"*/{run_id}.json"), reverse=True):
            with candidate.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        return None
