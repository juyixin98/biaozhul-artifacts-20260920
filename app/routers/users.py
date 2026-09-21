from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.schemas import UserCreate, UserOut

router = APIRouter(tags=["users"])


@router.post("/users", response_model=UserOut, status_code=201)
def create_user(payload: UserCreate, db: Session = Depends(get_db)):
    """Provisioning endpoint for the demo (identity would come from an
    external IdP in production)."""
    user = User(name=payload.name, role=payload.role)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.get("/users", response_model=list[UserOut])
def list_users(db: Session = Depends(get_db)):
    return list(db.scalars(select(User).order_by(User.id)).all())
