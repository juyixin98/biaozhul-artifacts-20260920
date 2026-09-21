from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Draft, User
from app.schemas import DraftCreate, DraftOut, DraftPatch
from app.security import get_current_user
from app import services

router = APIRouter(prefix="/drafts", tags=["drafts"])


@router.post("", response_model=DraftOut, status_code=201)
def create_draft(
    body: DraftCreate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> DraftOut:
    content = body.model_dump()
    draft = services.create_draft(
        db,
        user_id=user.id,
        content=content,
        request_id=request.state.request_id,
    )
    return DraftOut.model_validate(draft)


@router.get("", response_model=list[DraftOut])
def list_drafts(
    wallet_id: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[DraftOut]:
    stmt = select(Draft).where(Draft.user_id == user.id)
    if wallet_id:
        stmt = stmt.where(Draft.wallet_id == wallet_id)
    rows = db.scalars(stmt.order_by(Draft.created_at.desc())).all()
    return [DraftOut.model_validate(d) for d in rows]


@router.get("/{draft_id}", response_model=DraftOut)
def get_draft(
    draft_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> DraftOut:
    draft = db.get(Draft, draft_id)
    if draft is None or draft.user_id != user.id:
        from app.errors import NotFound

        raise NotFound("draft_not_found", "draft not found")
    return DraftOut.model_validate(draft)


@router.patch("/{draft_id}", response_model=DraftOut)
def patch_draft(
    draft_id: str,
    body: DraftPatch,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> DraftOut:
    patch = body.model_dump(exclude_unset=True)
    patch.pop("id", None)
    draft = services.update_draft(
        db, user_id=user.id, draft_id=draft_id, patch=patch
    )
    return DraftOut.model_validate(draft)


@router.delete("/{draft_id}", status_code=204)
def delete_draft(
    draft_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    services.delete_draft(db, user_id=user.id, draft_id=draft_id)
