"""Demo-grade role auth via headers.

Roles:
  - accountant:       post/reverse journals, import CSV, create/cancel expense requests
  - finance_officer:  everything an accountant can do, plus manage budgets/periods,
                      approve expense requests, close and reopen periods
  - auditor:          read-only

This is intentionally simple (X-User-Id / X-Role headers) so the authorization
matrix is easy to exercise in tests. It is NOT a production authentication scheme.
"""
from dataclasses import dataclass

from fastapi import Depends, Header

from .errors import DomainError

ACCOUNTANT = "accountant"
FINANCE_OFFICER = "finance_officer"
AUDITOR = "auditor"
ROLES = {ACCOUNTANT, FINANCE_OFFICER, AUDITOR}


@dataclass
class User:
    id: str
    role: str


def get_current_user(
    x_user_id: str = Header(default="anonymous"),
    x_role: str = Header(default="auditor"),
) -> User:
    if x_role not in ROLES:
        raise DomainError(401, f"unknown role: {x_role!r}")
    return User(id=x_user_id, role=x_role)


def require_roles(*roles: str):
    def dependency(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise DomainError(403, f"role {user.role!r} is not allowed to perform this action")
        return user

    return dependency
