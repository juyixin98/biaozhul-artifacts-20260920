"""Frozen configuration.

Everything that could otherwise become a magic value lives here. A
``Config`` is immutable (frozen dataclass): changing it produces a new
object, which matters because the stream master seed must never silently
change for an existing study.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DB_PATH = str(Path(__file__).resolve().parent.parent / "demo" / "demo.db")
DEFAULT_MASTER_SEED = 20260927
DEFAULT_API_TOKENS = "enrol-token:enroller|audit-token:auditor|admin-token:administrator"


@dataclass(frozen=True)
class Config:
    """Service configuration.

    Attributes
    ----------
    db_path:
        SQLite database file (``:memory:`` is allowed for tests).
    master_seed:
        Master seed from which every per-stratum random stream is derived.
        Frozen for the lifetime of a deployment; rotating it would change
        future allocations, so replay tooling records it explicitly.
    api_tokens:
        ``token:role`` pairs joined by ``|``. Roles: enroller, auditor,
        administrator.
    """

    db_path: str = DEFAULT_DB_PATH
    master_seed: int = DEFAULT_MASTER_SEED
    api_tokens: str = DEFAULT_API_TOKENS

    @staticmethod
    def from_env() -> "Config":
        return Config(
            db_path=os.environ.get("STRATBLOCK_DB", DEFAULT_DB_PATH),
            master_seed=int(os.environ.get("STRATBLOCK_MASTER_SEED", str(DEFAULT_MASTER_SEED))),
            api_tokens=os.environ.get("STRATBLOCK_API_TOKENS", DEFAULT_API_TOKENS),
        )

    def token_roles(self) -> dict[str, str]:
        """Parse ``t1:r1|t2:r2`` into ``{token: role}``."""
        mapping: dict[str, str] = {}
        for pair in self.api_tokens.split("|"):
            pair = pair.strip()
            if not pair:
                continue
            token, _, role = pair.partition(":")
            if not token or not role:
                raise ValueError(f"invalid token entry: {pair!r}")
            mapping[token] = role.strip()
        return mapping
