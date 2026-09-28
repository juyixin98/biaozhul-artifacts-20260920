"""本地种子保险库。

*合成实验*的种子持久化：JSON 文件（权限 0600），仅用于本机复现与
"进程重启后恢复"测试。生产部署中这里应替换为 KMS/HSM；本服务不依赖
任何外部账号。

保险库是服务进程内唯一持有种子明文的组件；API 层任何响应都不会
回读种子（见 :class:`SecretSeed` 的 ``__repr__`` 脱敏）。
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from ..errors import AppError, ErrorCategory
from .seed import SecretSeed


class SeedVault:
    def __init__(self, path: str | None):
        self._path = Path(path) if path and path != ":memory:" else None
        self._lock = threading.Lock()
        self._seeds: dict[str, SecretSeed] = {}
        if self._path and self._path.exists():
            self._load_locked()

    def _load_locked(self) -> None:
        data = json.loads(self._path.read_text(encoding="utf-8"))
        for study_id, entry in data.get("studies", {}).items():
            self._seeds[study_id] = SecretSeed.from_base64(entry["seed_b64"])

    def _persist_locked(self) -> None:
        if self._path is None:
            return
        import base64
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "warning": "LOCAL SYNTHETIC seed store (0600). 不适用于生产。",
            "studies": {
                study_id: {
                    "seed_b64": base64.b64encode(seed.material).decode("ascii"),
                    "fingerprint": seed.fingerprint(),
                }
                for study_id, seed in self._seeds.items()
            },
        }
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                      encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._path)
        os.chmod(self._path, 0o600)

    def store(self, study_id: str, seed: SecretSeed) -> None:
        with self._lock:
            if study_id in self._seeds:
                raise AppError(
                    ErrorCategory.STUDY_ALREADY_EXISTS, 409,
                    f"研究 {study_id!r} 的种子已在保险库中",
                )
            self._seeds[study_id] = seed
            self._persist_locked()

    def get(self, study_id: str) -> SecretSeed:
        with self._lock:
            seed = self._seeds.get(study_id)
        if seed is None:
            raise AppError(
                ErrorCategory.SEED_REQUIRED, 409,
                f"保险库中没有研究 {study_id!r} 的种子；"
                "无法在本机恢复分配随机流（请确认种子保险库文件）",
            )
        return seed

    def has(self, study_id: str) -> bool:
        with self._lock:
            return study_id in self._seeds

    def location(self) -> str:
        return str(self._path) if self._path else "memory-only"
