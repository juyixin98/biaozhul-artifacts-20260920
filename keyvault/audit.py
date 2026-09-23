"""审计日志：仅追加（append-only）的 JSONL，带 SHA-256 哈希链。

安全约束：
  * 绝不记录密钥材料、明文、密文原文；只记录版本号、事件类型、
    以及内容的 SHA-256 摘要（用于事后核对，不可逆推原文）；
  * 每条记录携带前一条记录的哈希，形成链，篡改或截断可被 verify() 发现；
  * 每条记录写入后立即 flush + fsync，崩溃最多丢失最后一条。
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

GENESIS_HASH = "0" * 64


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class AuditLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()
        self._seq, self._prev_hash = self._recover_tail()

    def _recover_tail(self) -> tuple[int, str]:
        """启动时读取最后一条有效记录，恢复序号与链头。"""
        seq, prev = 0, GENESIS_HASH
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    # 崩溃留下的半行：截断之，保证后续追加从干净位置开始
                    self._truncate_to_valid()
                    break
                seq, prev = rec["seq"], rec["hash"]
        return seq, prev

    def _truncate_to_valid(self) -> None:
        valid = []
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    json.loads(line)
                    valid.append(line)
                except json.JSONDecodeError:
                    break
        with open(self.path, "w", encoding="utf-8") as f:
            f.writelines(valid)
            f.flush()
            os.fsync(f.fileno())

    @staticmethod
    def _record_hash(rec: dict) -> str:
        body = {k: v for k, v in rec.items() if k != "hash"}
        blob = json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return sha256_hex(blob)

    def append(self, event: str, **details) -> dict:
        """追加一条审计记录。调用方不得传入密钥材料或明文。"""
        rec = {
            "seq": self._seq + 1,
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "details": details,
            "prev_hash": self._prev_hash,
        }
        rec["hash"] = self._record_hash(rec)
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
        self._seq, self._prev_hash = rec["seq"], rec["hash"]
        return rec

    def __iter__(self) -> Iterator[dict]:
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    yield json.loads(line)

    def verify(self) -> tuple[bool, Optional[str]]:
        """校验哈希链完整性。返回 (是否完整, 失败原因)。"""
        prev = GENESIS_HASH
        expected_seq = 1
        for rec in self:
            if rec["seq"] != expected_seq:
                return False, f"序号断裂: 期望 {expected_seq}, 实际 {rec['seq']}"
            if rec["prev_hash"] != prev:
                return False, f"第 {rec['seq']} 条 prev_hash 不匹配"
            if rec["hash"] != self._record_hash(rec):
                return False, f"第 {rec['seq']} 条哈希校验失败"
            prev = rec["hash"]
            expected_seq += 1
        return True, None
