import logging

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from . import services
from .db import get_db
from .models import AuditLog, SignRequest, User, Wallet
from .schemas import (
    AuditOut,
    DraftCreate,
    DraftOut,
    SignRequestCreate,
    SignRequestOut,
    UserOut,
    WalletCreate,
    WalletOut,
)
from .services import ApiError

logger = logging.getLogger("vaultcommand")

app = FastAPI(title="VaultCommand", version="1.0.0")


@app.exception_handler(ApiError)
async def api_error_handler(_: Request, exc: ApiError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})


@app.exception_handler(Exception)
async def unhandled_error_handler(_: Request, exc: Exception):
    # Generic on purpose: internals (and certainly key material) never leak.
    logger.error("unhandled error: %s", type(exc).__name__)
    return JSONResponse(status_code=500, content={"detail": "internal error"})


def current_user(
    x_api_key: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    if not x_api_key:
        raise ApiError(401, "missing API key")
    user = db.query(User).filter_by(api_key=x_api_key).one_or_none()
    if user is None:
        raise ApiError(401, "invalid API key")
    return user


# ------------------------------------------------------------------ helpers


def _wallet_out(w: Wallet) -> WalletOut:
    return WalletOut(
        id=w.id,
        kind=w.kind,
        address=w.address,
        last_signed_at=w.last_signed_at.isoformat() if w.last_signed_at else None,
        created_at=w.created_at.isoformat(),
    )


def _draft_out(d) -> DraftOut:
    return DraftOut(
        id=d.id,
        wallet_id=d.wallet_id,
        chain_id=d.chain_id,
        to_address=d.to_address,
        value_wei=str(int(d.value_wei)),
        gas_limit=d.gas_limit,
        gas_price_wei=str(int(d.gas_price_wei)),
        nonce=d.nonce,
        status=d.status,
        content_hash=d.content_hash,
        created_at=d.created_at.isoformat(),
        submitted_at=d.submitted_at.isoformat() if d.submitted_at else None,
    )


def _sign_out(r: SignRequest) -> SignRequestOut:
    return SignRequestOut(
        id=r.id,
        draft_id=r.draft_id,
        wallet_id=r.wallet_id,
        status=r.status,
        tx_hash=r.tx_hash,
        signed_tx=r.signed_tx,
        error=r.error,
        quota_reserved_wei=str(int(r.quota_reserved_wei)),
        quota_released=r.quota_released,
        created_at=r.created_at.isoformat(),
        updated_at=r.updated_at.isoformat(),
    )


# ------------------------------------------------------------------ routes


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/users", response_model=UserOut, status_code=201)
def create_user(db: Session = Depends(get_db)):
    user = services.create_user(db)
    return UserOut(user_id=user.id, api_key=user.api_key)


@app.post("/wallets", response_model=WalletOut, status_code=201)
def create_wallet(
    body: WalletCreate,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    wallet = services.create_wallet(db, user, body.kind, body.private_key, body.address)
    return _wallet_out(wallet)


@app.get("/wallets", response_model=list[WalletOut])
def list_wallets(db: Session = Depends(get_db), user: User = Depends(current_user)):
    return [_wallet_out(w) for w in db.query(Wallet).filter_by(user_id=user.id).all()]


@app.post("/drafts", response_model=DraftOut, status_code=201)
def create_draft(
    body: DraftCreate,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    return _draft_out(services.create_draft(db, user, body))


@app.get("/drafts/{draft_id}", response_model=DraftOut)
def get_draft(draft_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return _draft_out(services.get_owned_draft(db, user, draft_id))


@app.post("/drafts/{draft_id}/submit", response_model=DraftOut)
def submit_draft(draft_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return _draft_out(services.submit_draft(db, user, draft_id))


@app.post("/sign", response_model=SignRequestOut)
def sign(
    body: SignRequestCreate,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    req = services.request_signature(db, user, body.draft_id, idempotency_key)
    return _sign_out(req)


@app.get("/sign/{request_id}", response_model=SignRequestOut)
def get_sign_request(
    request_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    return _sign_out(services.get_owned_sign_request(db, user, request_id))


@app.get("/sign", response_model=list[SignRequestOut])
def list_sign_requests(db: Session = Depends(get_db), user: User = Depends(current_user)):
    rows = db.query(SignRequest).filter_by(user_id=user.id).order_by(SignRequest.created_at).all()
    return [_sign_out(r) for r in rows]


@app.get("/audit", response_model=list[AuditOut])
def list_audit(db: Session = Depends(get_db), user: User = Depends(current_user)):
    rows = db.query(AuditLog).filter_by(user_id=user.id).order_by(AuditLog.created_at).all()
    return [
        AuditOut(
            id=r.id,
            wallet_id=r.wallet_id,
            action=r.action,
            tx_digest=r.tx_digest,
            result=r.result,
            detail=r.detail,
            created_at=r.created_at.isoformat(),
        )
        for r in rows
    ]
