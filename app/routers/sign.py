from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.errors import NotFound
from app.models import SignRequest, User
from app.schemas import SignRequestOut, SignSubmit
from app.security import get_current_user
from app import services

router = APIRouter(prefix="/sign-requests", tags=["signing"])


def _serialize(req: SignRequest, *, replayed: bool = False) -> SignRequestOut:
    return SignRequestOut(
        id=req.id,
        draft_id=req.draft_id,
        wallet_id=req.wallet_id,
        idempotency_key=req.idempotency_key,
        status=req.status,
        chain_id=int(req.chain_id),
        to_address=req.to_address,
        value_wei=int(req.value_wei),
        gas=int(req.gas),
        gas_price_wei=int(req.gas_price_wei),
        nonce=int(req.nonce),
        data_hex=req.data_hex,
        raw_transaction_hex=req.raw_transaction_hex,
        tx_hash=req.tx_hash,
        error_reason=req.error_reason,
        replayed=replayed,
        created_at=req.created_at,
        signed_at=req.signed_at,
        finished_at=req.finished_at,
    )


@router.post("/{draft_id}/submit", response_model=SignRequestOut)
def submit(
    draft_id: str,
    body: SignSubmit,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> SignRequestOut:
    """Submit a draft for signing (idempotent). On a lost response after a
    successful call, retry with the SAME idempotency key to retrieve the
    original result."""
    req, replayed = services.submit_sign(
        db,
        user_id=user.id,
        draft_id=draft_id,
        idempotency_key=body.idempotency_key,
        request_id=request.state.request_id,
    )
    return _serialize(req, replayed=replayed)


@router.get("", response_model=list[SignRequestOut])
def list_requests(
    wallet_id: str | None = None,
    status_filter: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[SignRequestOut]:
    stmt = select(SignRequest).where(SignRequest.user_id == user.id)
    if wallet_id:
        stmt = stmt.where(SignRequest.wallet_id == wallet_id)
    if status_filter:
        stmt = stmt.where(SignRequest.status == status_filter)
    rows = db.scalars(stmt.order_by(SignRequest.created_at.desc())).all()
    return [_serialize(r) for r in rows]


@router.get("/{sign_request_id}", response_model=SignRequestOut)
def get_request(
    sign_request_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> SignRequestOut:
    req = db.get(SignRequest, sign_request_id)
    if req is None or req.user_id != user.id:
        raise NotFound("sign_request_not_found", "sign request not found")
    return _serialize(req)


@router.post("/{sign_request_id}/resume", response_model=SignRequestOut)
def resume(
    sign_request_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> SignRequestOut:
    """Recover a request left `pending` by an interrupted process. Safe to
    repeat: it finishes the same request under the same quota/nonce hold."""
    req = services.resume_sign(
        db,
        user_id=user.id,
        sign_request_id=sign_request_id,
        request_id=request.state.request_id,
    )
    return _serialize(req)


@router.post("/{sign_request_id}/release", response_model=SignRequestOut)
def release(
    sign_request_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> SignRequestOut:
    """Abandon a stuck `pending` request: release its daily-quota hold and free
    its nonce. No signature is ever produced for a released request."""
    req = services.release_pending(
        db,
        user_id=user.id,
        sign_request_id=sign_request_id,
        request_id=request.state.request_id,
    )
    return _serialize(req)
