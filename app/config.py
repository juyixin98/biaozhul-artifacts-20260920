"""本地合成配置。

所有凭证都是*本地合成夹具*：默认令牌仅用于本机验收，生产部署必须通过
环境变量/配置文件覆盖。不依赖任何外部账号。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

# 合成开发令牌（明确标注，非真实凭证）
DEFAULT_SYNTHETIC_TOKENS = {
    "synt-investigator-token": "investigator",
    "synt-auditor-token": "auditor",
    "synt-admin-token": "admin",
}

ROLES = ("investigator", "auditor", "admin")


@dataclass(frozen=True)
class Settings:
    database_path: str
    seeds_path: str
    tokens: dict[str, str]
    log_path: str | None
    pbkdf2_iterations: int
    env: str

    @property
    def sqlite_dsn(self) -> str:
        return self.database_path


def load_settings(config_path: str | None = None) -> Settings:
    config_path = config_path or os.environ.get("RCT_CONFIG")
    data: dict = {}
    if config_path and Path(config_path).exists():
        data = json.loads(Path(config_path).read_text(encoding="utf-8"))

    db_path = os.environ.get("RCT_DB") or data.get("database_path") or "./data/rct.db"
    seeds_path = (os.environ.get("RCT_SEEDS") or data.get("seeds_path")
                  or "./data/seeds.json")
    log_path = os.environ.get("RCT_LOG") or data.get("log_path") or "./data/audit.log"
    env = os.environ.get("RCT_ENV") or data.get("env") or "local-synthetic"

    tokens = dict(DEFAULT_SYNTHETIC_TOKENS)
    file_tokens = data.get("tokens")
    if isinstance(file_tokens, dict):
        tokens.update({str(k): str(v) for k, v in file_tokens.items()})
    env_tokens = os.environ.get("RCT_TOKENS_JSON")
    if env_tokens:
        tokens.update({str(k): str(v) for k, v in json.loads(env_tokens).items()})
    for role in tokens.values():
        if role not in ROLES:
            raise ValueError(f"配置中的角色非法: {role!r}，合法角色: {ROLES}")

    return Settings(
        database_path=db_path,
        seeds_path=seeds_path,
        tokens=tokens,
        log_path=log_path,
        pbkdf2_iterations=int(os.environ.get("RCT_PBKDF2_ITERATIONS", "200000")),
        env=env,
    )
