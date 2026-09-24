"""Gateway configuration loading.

Configuration is a local JSON file listing the issuers the gateway trusts.
Nothing about issuers, keys or audiences is ever taken from a request.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .jwtv.jwks import IssuerConfig

DEFAULT_CONFIG_PATH = "config/issuers.json"
ENV_CONFIG_PATH = "JWT_GATEWAY_CONFIG"


def load_issuer_configs(path: str | os.PathLike[str]) -> list[IssuerConfig]:
    file = Path(path)
    if not file.is_file():
        raise SystemExit(f"config file not found: {file}")
    try:
        raw: Any = json.loads(file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"config file {file} is not valid JSON: {exc}")
    if not isinstance(raw, dict) or not isinstance(raw.get("issuers"), list):
        raise SystemExit(f"config file {file} must contain an 'issuers' array")
    if not raw["issuers"]:
        raise SystemExit(f"config file {file} configures no issuers")
    configs: list[IssuerConfig] = []
    for entry in raw["issuers"]:
        if not isinstance(entry, dict):
            raise SystemExit(f"config file {file}: issuer entries must be objects")
        try:
            configs.append(IssuerConfig.from_dict(entry))
        except ValueError as exc:
            raise SystemExit(f"config file {file}: {exc}")
    return configs


def resolve_config_path() -> str:
    return os.environ.get(ENV_CONFIG_PATH, DEFAULT_CONFIG_PATH)
