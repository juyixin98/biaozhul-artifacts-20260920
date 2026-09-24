"""拓扑快照持久化：真实 SHA-256 内容哈希 + HMAC-SHA256 签名。

* payload 为快照的规范化 JSON（sort_keys=True，UTF-8 编码）。
* sha256      = SHA-256(payload)，检测任何字节级改动。
* hmac_sha256 = HMAC-SHA256(key, payload)，防止知道哈希算法的攻击者伪造内容。
  密钥解析顺序：环境变量 QOSDIAG_HMAC_KEY > <data_dir>/.hmac_key（0600，首次自动生成）
  > 开发默认值（仅本地使用，README 中说明）。
加载快照时两个值都重新计算并比对（constant_time_compare），任何篡改都会被拒绝。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .models import Snapshot, SnapshotEnvelope, Topology
from .rules import RULES_VERSION, diagnose_topology

_DEV_DEFAULT_KEY = b"qos-diag-dev-key-do-not-use-in-production"
KEY_FILE_NAME = ".hmac_key"


def _resolve_key(data_dir: Path) -> tuple[bytes, str]:
    env = os.environ.get("QOSDIAG_HMAC_KEY")
    if env:
        return env.encode("utf-8"), "env:QOSDIAG_HMAC_KEY"
    key_file = data_dir / KEY_FILE_NAME
    if key_file.exists():
        return key_file.read_bytes().strip(), f"file:{KEY_FILE_NAME}"
    generated = secrets.token_bytes(32)
    key_file.write_bytes(generated)
    os.chmod(key_file, 0o600)
    return generated, f"file:{KEY_FILE_NAME}(generated)"


def canonical_payload(snapshot: Snapshot) -> bytes:
    """规范化序列化：保证同样内容字节一致（哈希/签名可复现）。"""
    return json.dumps(
        snapshot.model_dump(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


class SnapshotStore:
    def __init__(self, data_dir: str | os.PathLike[str]):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._key, self.key_id = _resolve_key(self.data_dir)

    # --- 真实密码学原语 ---
    def sha256(self, payload: bytes) -> str:
        return hashlib.sha256(payload).hexdigest()

    def hmac_sign(self, payload: bytes) -> str:
        return hmac.new(self._key, payload, hashlib.sha256).hexdigest()

    def create_snapshot(self, topology: Topology) -> Snapshot:
        now = time.time()
        return Snapshot(
            snapshot_id=uuid.uuid4().hex[:12],
            created_at=now,
            created_iso=datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
            rules_version=RULES_VERSION,
            topology=topology,
            diagnoses=diagnose_topology(topology),
        )

    def save(self, snapshot: Snapshot) -> Path:
        payload = canonical_payload(snapshot)
        envelope = SnapshotEnvelope(
            snapshot_id=snapshot.snapshot_id,
            sha256=self.sha256(payload),
            hmac_sha256=self.hmac_sign(payload),
            hmac_key_id=self.key_id,
            payload=payload.decode("utf-8"),
        )
        target = self.data_dir / f"snapshot-{snapshot.snapshot_id}.json"
        # 原子写：同目录临时文件 + os.replace
        fd, tmp_name = tempfile.mkstemp(prefix=".snap-", suffix=".tmp", dir=self.data_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(envelope.model_dump(), fh, ensure_ascii=False, indent=2)
            os.replace(tmp_name, target)
        except BaseException:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise
        return target

    def list_snapshots(self) -> list[dict[str, object]]:
        result = []
        for p in sorted(self.data_dir.glob("snapshot-*.json"), key=lambda x: x.stat().st_mtime):
            try:
                envelope = self.load(p.name)
                result.append(
                    {
                        "file": p.name,
                        "snapshot_id": envelope.snapshot_id,
                        "created_iso": envelope.created_iso,
                        "rules_version": envelope.rules_version,
                        "size_bytes": p.stat().st_size,
                    }
                )
            except ValueError:
                result.append({"file": p.name, "error": "integrity verification failed"})
        return result

    def load(self, file_name: str) -> Snapshot:
        """加载并校验快照。文件名做了路径穿越防护；哈希或 HMAC 不匹配即抛错。"""
        path = (self.data_dir / file_name).resolve()
        if self.data_dir.resolve() not in path.parents and path.parent != self.data_dir.resolve():
            raise ValueError("illegal snapshot path")
        raw = json.loads(path.read_text(encoding="utf-8"))
        envelope = SnapshotEnvelope.model_validate(raw)
        payload = envelope.payload.encode("utf-8")

        actual_sha = self.sha256(payload)
        if not hmac.compare_digest(actual_sha, envelope.sha256):
            raise ValueError(
                f"snapshot {envelope.snapshot_id} 完整性校验失败：SHA-256 不匹配（内容可能被篡改）"
            )
        actual_hmac = self.hmac_sign(payload)
        if not hmac.compare_digest(actual_hmac, envelope.hmac_sha256):
            raise ValueError(
                f"snapshot {envelope.snapshot_id} 签名校验失败：HMAC-SHA256 不匹配（密钥不符或内容被伪造）"
            )
        return Snapshot.model_validate(json.loads(payload))
