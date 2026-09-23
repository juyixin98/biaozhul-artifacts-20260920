"""应用配置（环境变量）。"""
from __future__ import annotations

import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    database_url: str = "postgresql+psycopg2://inbox:inbox@localhost:55432/inbox"
    # 快照 / 证据落盘根目录
    snapshot_dir: str = str(Path(__file__).resolve().parent.parent / "data" / "snapshots")
    evidence_dir: str = str(Path(__file__).resolve().parent.parent / "data" / "evidence")
    # 管理令牌：为空则不鉴权（本地演示）；非空时管理类接口需 Authorization: Bearer <token>
    admin_token: str = ""


def get_settings() -> Settings:
    return Settings()


# 测试入口（tests/conftest.py 会 monkeypatch DATABASE_URL）
def database_url() -> str:
    return os.environ.get("DATABASE_URL", get_settings().database_url)
