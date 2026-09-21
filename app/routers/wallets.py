from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User, Wallet
from app.schemas import WalletCreate, WalletOut
from app.security import get_current_user
from app import services

router = APIRouter(prefix="/wallets", tags=["wallets"])


def _serialize(w: Wallet) -> WalletOut:
    return WalletOut(
        id=w.id,
        label=w.label,
        kind=w.kind,
        address=w.address,
        created_at=w.created_at,
        has_private_key=w.key_ciphertext is not None,
    )


@router.post("", response_model=WalletOut, status_code=201)
def create_wallet(
    body: WalletCreate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> WalletOut:
    wallet = services.create_wallet(
        db,
        user_id=user.id,
        label=body.label,
        kind=body.kind,
        private_key_hex=body.private_key_hex,
        address=body.address,
        request_id=request.state.request_id,
    )
    return _serialize(wallet)


@router.get("", response_model=list[WalletOut])
def list_wallets(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> list[WalletOut]:
    rows = db.scalars(
        select(Wallet).where(Wallet.user_id == user.id).order_by(Wallet.created_at)
    ).all()
    return [_serialize(w) for w in rows]


@router.get("/{wallet_id}", response_model=WalletOut)
def get_wallet(
    wallet_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> WalletOut:
    w = services._get_owned_wallet(db, user_id=user.id, wallet_id=wallet_id)
    return _serialize(w)
