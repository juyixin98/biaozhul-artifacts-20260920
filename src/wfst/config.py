"""配置层：全部来自环境变量，启动时校验，缺失即快速失败。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"环境变量 {name}={raw!r} 不是整数") from exc


@dataclass(frozen=True, slots=True)
class Settings:
    db_path: Path
    default_k: int
    default_budget: int
    max_input_len: int
    smoothing_count: float
    log_dir: Path

    @classmethod
    def from_env(cls) -> "Settings":
        default_db = Path(__file__).resolve().parents[2] / "data" / "wfst.db"
        default_log = Path(__file__).resolve().parents[2] / "results"
        s = cls(
            db_path=Path(os.environ.get("WFST_DB_PATH", str(default_db))),
            default_k=_env_int("WFST_DEFAULT_K", 5),
            default_budget=_env_int("WFST_DEFAULT_BUDGET", 200_000),
            max_input_len=_env_int("WFST_MAX_INPUT_LEN", 64),
            smoothing_count=float(os.environ.get("WFST_SMOOTHING_COUNT", "0.5")),
            log_dir=Path(os.environ.get("WFST_LOG_DIR", str(default_log))),
        )
        if s.default_k <= 0:
            raise ValueError("WFST_DEFAULT_K 必须为正")
        if s.default_budget <= 0:
            raise ValueError("WFST_DEFAULT_BUDGET 必须为正")
        if s.max_input_len <= 0:
            raise ValueError("WFST_MAX_INPUT_LEN 必须为正")
        if s.smoothing_count < 0:
            raise ValueError("WFST_SMOOTHING_COUNT 不能为负")
        s.db_path.parent.mkdir(parents=True, exist_ok=True)
        s.log_dir.mkdir(parents=True, exist_ok=True)
        return s
