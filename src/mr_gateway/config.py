"""Runtime configuration loaded from environment variables.

Everything here has an insecure-but-runnable default so that ``uvicorn`` starts
out of the box on a developer laptop; production deployments must override the
secrets via environment (see README).
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return int(raw)


@dataclass(frozen=True)
class Settings:
    # Symmetric HMAC secret used to sign/verify robot authorization tokens.
    hmac_secret: str
    # Shared bearer secret guarding the administrative (registration) API.
    admin_token: str
    # Default lifetime, in seconds, of freshly issued authorization tokens.
    default_ttl_seconds: int
    # Append-only audit log path; reasons for dropped/rejected commands are
    # persisted here as one JSON object per line.
    audit_log_path: str
    host: str
    port: int

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            hmac_secret=os.environ.get(
                "MRGW_HMAC_SECRET", "dev-only-hmac-secret-change-me"
            ),
            admin_token=os.environ.get(
                "MRGW_ADMIN_TOKEN", "dev-admin-token-change-me"
            ),
            default_ttl_seconds=_int("MRGW_TOKEN_TTL_SECONDS", 3600),
            audit_log_path=os.environ.get("MRGW_AUDIT_LOG", "mr_gateway_audit.jsonl"),
            host=os.environ.get("MRGW_HOST", "0.0.0.0"),
            port=_int("MRGW_PORT", 8000),
        )
