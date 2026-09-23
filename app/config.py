"""运行期配置 (全部可通过环境变量覆盖)。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    # PostgreSQL 连接串
    database_url: str = field(
        default_factory=lambda: _env(
            "LIQREPLAY_DATABASE_URL",
            "postgresql://liqreplay:liqreplay@127.0.0.1:55460/liqreplay",
        )
    )
    # 受信任操作者 Ed25519 公钥 (十六进制, 逗号分隔, 允许空白)
    operator_keys: str = field(
        default_factory=lambda: _env("LIQREPLAY_OPERATOR_KEYS", "")
    )
    # 服务端签名密钥目录 (首次启动自动生成)
    keys_dir: Path = field(
        default_factory=lambda: Path(_env("LIQREPLAY_KEYS_DIR", "data/keys"))
    )

    # ---- 协议常量 ----
    # 预言机价格陈旧阈值: 严格 > 60 秒则视为陈旧
    price_staleness_seconds: int = field(
        default_factory=lambda: int(_env("LIQREPLAY_STALENESS_SECONDS", "60"))
    )
    # 单次清算最多偿还当前债务的 50%
    max_repay_fraction_num: int = 1
    max_repay_fraction_den: int = 2
    # 清算奖励: 偿还者获得等值债务的 1.08 倍抵押品
    liquidation_bonus_num: int = field(
        default_factory=lambda: int(_env("LIQREPLAY_BONUS_NUM", "108"))
    )
    liquidation_bonus_den: int = field(
        default_factory=lambda: int(_env("LIQREPLAY_BONUS_DEN", "100"))
    )
    # 365 天通用年的秒数, 年化秒/秒利率换算用
    seconds_per_year: int = 365 * 24 * 60 * 60

    def operator_key_list(self) -> list[str]:
        return [k.strip().lower() for k in self.operator_keys.split(",") if k.strip()]


settings = Settings()
