"""应用配置：数据库连接与对账参数，均可通过环境变量覆盖。"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="RECON_", extra="ignore")

    # SQLAlchemy 连接串，例如 postgresql+psycopg2://recon:recon@127.0.0.1:5432/asset_recon
    database_url: str = "postgresql+psycopg2://recon:recon@127.0.0.1:5432/asset_recon"

    # 事件进入正式账前所需的确认数（各链可在 PUT /chains/{chain}/head 中单独覆盖）
    default_confirmations: int = 12

    # 配对超时（按对方链的区块高度计）：锁定后超过该高度仍未见铸造即判为超时未配对
    pairing_timeout_blocks: int = 100


settings = Settings()
