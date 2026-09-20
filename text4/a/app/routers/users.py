from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..clock import utcnow
from ..database import get_db
from ..deps import current_user
from ..errors import conflict
from ..models import User
from ..schemas import UserCreate, UserOut
from ..serializers import user_out

router = APIRouter(prefix="/users", tags=["users"])


@router.post("", response_model=UserOut, status_code=201)
def create_user(payload: UserCreate, db: Session = Depends(get_db)) -> UserOut:
    existing = db.scalar(select(User).where(User.email == payload.email))
    if existing is not None:
        raise conflict("USER_EXISTS", f"{payload.email} is already registered")
    user = User(
        email=payload.email,
        name=payload.name,
        role=payload.role,
        created_at=utcnow(db),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user_out(user)


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(current_user)) -> UserOut:
    return user_out(user)
