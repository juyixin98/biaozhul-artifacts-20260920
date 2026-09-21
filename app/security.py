from dataclasses import dataclass

from fastapi import Depends, Header

from .errors import DomainError

ROLE_LEARNER = "learner"
ROLE_SUPERVISOR = "supervisor"


@dataclass
class CurrentUser:
    user_id: str
    role: str

    @property
    def is_supervisor(self) -> bool:
        return self.role == ROLE_SUPERVISOR


def get_current_user(
    x_user_id: str = Header(..., alias="X-User-Id"),
    x_user_role: str = Header(ROLE_LEARNER, alias="X-User-Role"),
) -> CurrentUser:
    if x_user_role not in (ROLE_LEARNER, ROLE_SUPERVISOR):
        raise DomainError(400, f"unknown role: {x_user_role}")
    return CurrentUser(user_id=x_user_id, role=x_user_role)


def require_supervisor(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if not user.is_supervisor:
        raise DomainError(403, "supervisor role required")
    return user
