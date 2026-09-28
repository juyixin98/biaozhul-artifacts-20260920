"""诊断日志配置。

测试日志与服务日志都经过这里：带运行身份（run id）、时间戳、级别，
并输出到文件，便于把日志与具体输入/运行关联。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

_CONFIGURED: set[str] = set()


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def new_run_id() -> str:
    """生成可关联一次运行的短身份，如 ``20260927T...-ab12cd``。"""
    return f"{utc_stamp()}-{uuid.uuid4().hex[:6]}"


def configure_logging(log_dir: Path | str, level: str = "INFO", run_id: str | None = None) -> str:
    """配置根 logger，返回本次运行身份。"""
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    run_id = run_id or new_run_id()
    if run_id in _CONFIGURED:
        return run_id

    log_path = log_dir / f"run-{run_id}.log"
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | run=" + run_id + " | %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)
    # 避免重复添加 handler（pytest 多次导入时）。
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(stream_handler)

    _CONFIGURED.add(run_id)
    logging.getLogger(__name__).info(
        "logging configured run_id=%s file=%s level=%s", run_id, log_path, level
    )
    return run_id
