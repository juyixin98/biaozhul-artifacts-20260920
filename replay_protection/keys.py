"""Local test key registry.

Keys are generated with the OS CSPRNG via ``secrets`` and stored in a local
JSON file. Nothing here touches production accounts or key services.
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

KEY_FILE_MODE = 0o600
SECRET_BYTES = 32  # 256-bit HMAC keys


class KeyRegistry:
    def __init__(self, keys: dict[str, bytes]):
        self._keys = dict(keys)

    @classmethod
    def load(cls, path: str | os.PathLike) -> "KeyRegistry":
        p = Path(path)
        raw = json.loads(p.read_text(encoding="utf-8"))
        keys = {entry["kid"]: bytes.fromhex(entry["secret_hex"]) for entry in raw["keys"]}
        return cls(keys)

    def get(self, kid: str) -> bytes | None:
        return self._keys.get(kid)

    def has(self, kid: str) -> bool:
        return kid in self._keys

    def ids(self) -> list[str]:
        return sorted(self._keys)


def generate_key() -> tuple[str, str]:
    """Return a fresh (kid, secret_hex) pair for local testing."""
    kid = "k-" + secrets.token_hex(8)
    secret_hex = secrets.token_hex(SECRET_BYTES)
    return kid, secret_hex


def write_key_file(path: str | os.PathLike, entries: list[tuple[str, str]]) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "keys": [
            {"kid": kid, "secret_hex": secret_hex, "algorithm": "HMAC-SHA256"}
            for kid, secret_hex in entries
        ]
    }
    p.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.chmod(p, KEY_FILE_MODE)
    return p
