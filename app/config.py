"""服务配置。数据目录可通过环境变量 SNAPSHOT_HOME 覆盖。"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_HOME = Path(os.environ.get("SNAPSHOT_HOME", "./data"))


class Settings:
    def __init__(self, home: Path | str | None = None) -> None:
        self.home = Path(home) if home is not None else DEFAULT_HOME
        self.db_path = self.home / "snapshots.db"
        self.evidence_dir = self.home / "evidence"
        self.key_path = self.home / "hmac.key"

    def ensure_dirs(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        self.evidence_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
