"""应用配置（全部可通过环境变量覆盖）。"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


@dataclass(frozen=True)
class Settings:
    # 数据库：postgresql+asyncpg://... ；留空则使用进程内内存库（便于快速测试）
    database_url: str = _env("DATABASE_URL", "")

    # 事件签名开关。默认验证 Ed25519 签名；演示密钥随仓库提供，可用生成脚本更换。
    require_signatures: bool = _env("REQUIRE_SIGNATURES", "1") != "0"
    feeder_public_key_hex: str = _env(
        "FEEDER_PUBLIC_KEY_HEX", ""
    )  # 为空时回退到 keys/feeder_public.hex
    server_private_key_hex: str = _env(
        "SERVER_PRIVATE_KEY_HEX", ""
    )  # 为空时回退到 keys/server_private.hex（报告签名）
    keys_dir: str = _env("KEYS_DIR", os.path.join(os.path.dirname(__file__), "..", "keys"))

    # 协议参数（定点单位 WAD = 1e18）
    seconds_per_year: int = int(_env("SECONDS_PER_YEAR", str(365 * 24 * 3600)))
    price_staleness_seconds: int = int(_env("PRICE_STALENESS_SECONDS", "60"))
    max_repay_fraction_num: int = int(_env("MAX_REPAY_NUM", "1"))
    max_repay_fraction_den: int = int(_env("MAX_REPAY_DEN", "2"))

    # 开发/测试重置接口开关
    allow_reset: bool = _env("ALLOW_RESET", "0") == "1"


settings = Settings()
