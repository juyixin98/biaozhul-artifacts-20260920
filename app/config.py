"""独立配置层。

所有路径、日志与服务参数集中在此，可用环境变量覆盖；配置对象不可变。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_LOG_DIR = PROJECT_ROOT / "logs"
DEFAULT_DB_NAME = "dawg_index.sqlite3"


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"环境变量 {name} 必须是整数，实际为 {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    """运行配置（不可变）。"""

    db_path: Path
    log_dir: Path
    log_level: str
    host: str
    port: int
    default_index_name: str

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(os.environ.get("DAWG_DATA_DIR", str(DEFAULT_DATA_DIR)))
        db_name = os.environ.get("DAWG_DB_NAME", DEFAULT_DB_NAME)
        log_dir = Path(os.environ.get("DAWG_LOG_DIR", str(DEFAULT_LOG_DIR)))
        return cls(
            db_path=data_dir / db_name,
            log_dir=log_dir,
            log_level=os.environ.get("DAWG_LOG_LEVEL", "INFO").upper(),
            host=os.environ.get("DAWG_HOST", "127.0.0.1"),
            port=_env_int("DAWG_PORT", 8000),
            default_index_name=os.environ.get("DAWG_INDEX_NAME", "default"),
        )


SETTINGS = Settings.from_env()
