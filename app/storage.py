"""追加式 JSONL 存储。

entries.jsonl     每行: {"entry": {...}, "entry_hash": "sha256:..."}
checkpoints.jsonl 每行: {"checkpoint": {...}, "signature": "<base64url>"}

只追加、不修改。进程内用锁保证追加与读快照的一致性。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Optional

ENTRIES_FILE = "entries.jsonl"
CHECKPOINTS_FILE = "checkpoints.jsonl"


class LogStore:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._entries_path = self.data_dir / ENTRIES_FILE
        self._checkpoints_path = self.data_dir / CHECKPOINTS_FILE
        self._lock = threading.Lock()

    # ---- entries ----

    def append_entry(self, entry: dict, entry_hash: str) -> None:
        line = json.dumps(
            {"entry": entry, "entry_hash": entry_hash},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        with self._lock:
            with self._entries_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")

    def read_entries(self) -> list[dict]:
        """返回全部 [{"entry":..., "entry_hash":...}]，按存储顺序。"""
        if not self._entries_path.exists():
            return []
        out = []
        with self._lock:
            with self._entries_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        out.append(json.loads(line))
        return out

    def head(self) -> tuple[int, Optional[str]]:
        """当前链头 (seq, entry_hash)；空链返回 (0, None)。"""
        entries = self.read_entries()
        if not entries:
            return 0, None
        last = entries[-1]
        return last["entry"]["seq"], last["entry_hash"]

    # ---- checkpoints ----

    def append_checkpoint(self, checkpoint: dict, signature: str) -> None:
        line = json.dumps(
            {"checkpoint": checkpoint, "signature": signature},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        with self._lock:
            with self._checkpoints_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")

    def read_checkpoints(self) -> list[dict]:
        if not self._checkpoints_path.exists():
            return []
        out = []
        with self._lock:
            with self._checkpoints_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        out.append(json.loads(line))
        return out

    def latest_checkpoint(self) -> Optional[dict]:
        cps = self.read_checkpoints()
        return cps[-1] if cps else None
