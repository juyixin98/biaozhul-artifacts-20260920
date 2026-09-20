"""Header-based role auth for the sample deployment.

This is intentionally simple (a gateway/real IdP would sit in front in
production). Roles:

- clerk            post journals, import CSV, reverse entries
- approver         approve/cancel encumbrances
- finance_manager  everything above + manage budgets/periods, close/reopen
- auditor          read-only
"""

from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException

ROLES = ("clerk", "approver", "finance_manager", "auditor")


@dataclass
class CurrentUser:
    username: str
    role: str


def get_current_user(
    x_user_name: str | None = Header(default=None),
    x_user_role: str | None = Header(default=None),
) -> CurrentUser:
    role = (x_user_role or "auditor").strip()
    if role not in ROLES:
        raise HTTPException(status_code=401, detail=f"unknown role: {role!r}")
    return CurrentUser(username=(x_user_name or "anonymous").strip(), role=role)


def require_role(*roles: str):
    def dependency(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if user.role not in roles:
            raise HTTPException(
                status_code=403,
                detail=f"requires role: {' or '.join(roles)}",
            )
        return user

    return dependency
