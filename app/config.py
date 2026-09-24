"""运行配置（全部来自环境变量）。"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    data_dir: str
    bootstrap_root_public: str | None
    admin_token: str | None

    @classmethod
    def from_env(cls) -> "Settings":
        bootstrap = os.environ.get("BOOTSTRAP_ROOT_PUBLIC") or None
        if bootstrap:
            bootstrap = bootstrap.strip().lower()
        return cls(
            data_dir=os.environ.get("DATA_DIR", "./data"),
            bootstrap_root_public=bootstrap,
            admin_token=(os.environ.get("ADMIN_TOKEN") or None),
        )
