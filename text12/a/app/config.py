from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CLOUDGATE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "postgresql+psycopg2://cloudgate:cloudgate@localhost:5432/cloudgate"
    # 超级管理员密钥（用于创建租户）。生产环境必须通过环境变量覆盖。
    super_admin_key: str = "dev-super-key-change-me"

    # 心跳周期（仅用于告知设备，服务端不强制）
    heartbeat_interval_seconds: int = 60
    # 超过该时长没有心跳即判定租约过期
    heartbeat_timeout_seconds: int = 600
    # 后台清理扫描间隔；<=0 表示不启动后台扫描（测试用）
    reaper_interval_seconds: int = 30


settings = Settings()
