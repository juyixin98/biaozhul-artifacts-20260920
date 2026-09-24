"""Structured verification errors.

Every rejection raises :class:`TokenError` carrying a stable machine-readable
``code`` plus non-secret ``details``.  Details must never contain the raw
token, its signature, or claim values beyond what is strictly necessary to
explain the rejection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TokenError(Exception):
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    status_code: int = 401

    def __str__(self) -> str:  # pragma: no cover - debug representation
        return f"{self.code}: {self.message}"

    def to_body(self) -> dict[str, Any]:
        return {
            "status": "error",
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
        }


def reject(code: str, message: str, status_code: int = 401, **details: Any) -> TokenError:
    return TokenError(code=code, message=message, details=details, status_code=status_code)
