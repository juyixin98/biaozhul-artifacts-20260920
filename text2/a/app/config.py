from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration. Values can be overridden via environment variables
    (e.g. DATABASE_URL, OFFER_TTL_MINUTES)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg2://careforce:careforce@localhost:5432/careforce"
    offer_ttl_minutes: int = 8          # 邀请 8 分钟未接受则失效
    generation_horizon_days: int = 14   # 生成未来 14 天任务
    weekly_hour_limit: float = 44.0     # 周工时上限
    min_rest_hours: float = 10.0        # 班次间最小休息时长


settings = Settings()
