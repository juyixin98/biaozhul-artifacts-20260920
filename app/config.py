from __future__ import annotations

import os
from dataclasses import dataclass

# Development keys. In production supply your own keys via CIVICLEDGER_API_KEYS
# as a comma separated list of "role:key" entries.
DEFAULT_API_KEYS = {
    "lead": "dev-key-lead",
    "accountant": "dev-key-accountant",
    "auditor": "dev-key-auditor",
}


def _load_api_keys() -> dict[str, str]:
    raw = os.getenv("CIVICLEDGER_API_KEYS", "").strip()
    if not raw:
        return dict(DEFAULT_API_KEYS)
    keys: dict[str, str] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        role, _, key = item.partition(":")
        if role not in ("lead", "accountant", "auditor") or not key:
            raise RuntimeError(f"Invalid CIVICLEDGER_API_KEYS entry: {item!r}")
        keys[role] = key
    return keys


@dataclass(frozen=True)
class Settings:
    api_keys: dict[str, str]
    max_csv_rows: int = 2000
    # 10 trillion cents = 100 billion major units; totals stay well within BIGINT.
    max_amount_cents: int = 10_000_000_000_000


settings = Settings(api_keys=_load_api_keys())
