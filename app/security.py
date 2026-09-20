from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Header

from .config import settings
from .errors import AuthError, PermissionError

ROLES = ("lead", "accountant", "auditor")


@dataclass(frozen=True)
class Principal:
    role: str

    @property
    def name(self) -> str:
        return self.role


def get_principal(x_api_key: str | None = Header(default=None)) -> Principal:
    if not x_api_key:
        raise AuthError("Missing X-API-Key header")
    for role, key in settings.api_keys.items():
        if key == x_api_key:
            return Principal(role=role)
    raise AuthError("Invalid API key")


def require_roles(*roles: str):
    allowed = set(roles)

    def _check(principal: Principal = Depends(get_principal)) -> Principal:
        if principal.role not in allowed:
            raise PermissionError(
                f"Role '{principal.role}' may not perform this action"
            )
        return principal

    return _check
