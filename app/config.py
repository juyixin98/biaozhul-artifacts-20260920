"""服务配置：默认值 + 环境变量覆盖（前缀 SUMCMP_）+ 可选 YAML。

配置与代码分离：config/default.yaml 是声明式默认，环境变量优先。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "default.yaml"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SUMCMP_")

    host: str = "127.0.0.1"
    port: int = 8429
    log_level: str = "INFO"
    max_values: int = 2_000_000  # 直接提交数组的上限；更大输入走 generator
    default_chunk_size: int = 1024
    reference_dps: int = 50  # mpmath 十进制精度
    pairwise_block: int = 8


@lru_cache
def get_settings() -> Settings:
    overrides: dict = {}
    if _CONFIG_PATH.exists():
        overrides = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    return Settings(**overrides)
