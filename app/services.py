"""Business logic for VaultCommand.

Concurrency / recovery invariants
---------------------------------
* Quota check-and-reserve is a single atomic conditional UPDATE against the
  per-(wallet, day) usage row, so concurrent signers can never overspend.
* The pending sign-request row and its quota reservation commit in the same
  transaction: a crash can never leave reserved quota without a resumable row.
* Signing is deterministic (RFC 6979). A retry of a `pending` request re-signs
  and produces the *identical* payload, so a lost success response can never
  yield a second, different result.
* Quota is released only when a request reaches `failed`. Successful and
  still-pending requests keep their reservation. Release is idempotent
  (`quota_released` flag + guarded UPDATE).
* Nonce uniqueness per (wallet, chain) is enforced under a wallet row lock
  (SELECT ... FOR UPDATE on PostgreSQL) for non-failed requests.
"""

import hashlib
import json
import secrets
from datetime import datetime, timezone
from uuid import uuid4

from eth_utils import to_checksum_address
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from . import signing
from .config import settings
from .models import AuditLog, DailyUsage, Draft, SignRequest, User, Wallet
from .security import decrypt_secret, encrypt_secret
from .signing import sign_evm_transaction  # re-exported for test monkeypatching


class ApiError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def utcnow() -> datetime:
    # naive UTC everywhere so SQLite and PostgreSQL behave identically
    return datetime.now(timezone.utc).replace(tzinfo=None)


def canonical_hash(content: dict) -> str:
    blob = json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


def audit(db, user_id, wallet_id, action, tx_digest, result, detail=None):
    db.add(
        AuditLog(
            id=str(uuid4()),
            user_id=user_id,
            wallet_id=wallet_id,
            action=action,
            tx_digest=tx_digest,
            result=result,
            detail=detail,
            created_at=utcnow(),
        )
    )


# ---------------------------------------------------------------- users


def create_user(db) -> User:
    user = User(id=str(uuid4()), api_key="vc_" + secrets.token_hex(24), created_at=utcnow())
    db.add(user)
    db.commit()
    return user


# ---------------------------------------------------------------- wallets


def create_wallet(db, user: User, kind: str, private_key: str | None, address: str | None) -> Wallet:
    wallet_id = str(uuid4())
    if kind == "custodial":
        priv = private_key or signing.generate_private_key()
        try:
            addr = signing.address_from_private_key(priv)
        except Exception:
            raise ApiError(422, "invalid private key")
        # AAD binds the ciphertext to this wallet row.
        encrypted = encrypt_secret(priv.encode(), aad=wallet_id.encode())
    elif kind == "watch_only":
        if not address:
            raise ApiError(422, "watch-only wallet requires an address")
        try:
            addr = to_checksum_address(address)
        except ValueError:
            raise ApiError(422, "invalid address")
        encrypted = None
    else:
        raise ApiError(422, "kind must be 'custodial' or 'watch_only'")

    wallet = Wallet(
        id=wallet_id,
        user_id=user.id,
        address=addr,
        kind=kind,
        encrypted_key=encrypted,
        last_signed_at=None,
        created_at=utcnow(),
    )
    db.add(wallet)
    audit(db, user.id, wallet_id, "wallet_created", None, "ok", f"kind={kind}")
    db.commit()
    return wallet


def get_owned_wallet(db, user: User, wallet_id: str) -> Wallet:
    wallet = db.get(Wallet, wallet_id)
    if wallet is None or wallet.user_id != user.id:
        raise ApiError(404, "wallet not found")
    return wallet


# ---------------------------------------------------------------- drafts


def create_draft(db, user: User, data) -> Draft:
    wallet = get_owned_wallet(db, user, data.wallet_id)
    draft = Draft(
        id=str(uuid4()),
        user_id=user.id,
        wallet_id=wallet.id,
        chain_id=data.chain_id,
        to_address=data.to_address,
        value_wei=int(data.value_wei),
        gas_limit=data.gas_limit,
        gas_price_wei=int(data.gas_price_wei),
        nonce=data.nonce,
        status="draft",
        content_hash=None,
        created_at=utcnow(),
        submitted_at=None,
    )
    db.add(draft)
    audit(db, user.id, wallet.id, "draft_created", None, "ok", f"draft={draft.id}")
    db.commit()
    return draft


def get_owned_draft(db, user: User, draft_id: str) -> Draft:
    draft = db.get(Draft, draft_id)
    if draft is None or draft.user_id != user.id:
        raise ApiError(404, "draft not found")
    return draft


def _draft_content(draft: Draft) -> dict:
    return {
        "wallet_id": draft.wallet_id,
        "chain_id": draft.chain_id,
        "to_address": draft.to_address,
        "value_wei": str(int(draft.value_wei)),
        "gas_limit": draft.gas_limit,
        "gas_price_wei": str(int(draft.gas_price_wei)),
        "nonce": draft.nonce,
    }


def submit_draft(db, user: User, draft_id: str) -> Draft:
    """Freeze the draft: after submit there is no way to mutate its content."""
    draft = get_owned_draft(db, user, draft_id)
    if draft.status == "submitted":
        raise ApiError(409, "draft already submitted and frozen")
    draft.status = "submitted"
    draft.content_hash = canonical_hash(_draft_content(draft))
    draft.submitted_at = utcnow()
    audit(db, user.id, draft.wallet_id, "draft_submitted", None, "ok", f"draft={draft.id}")
    db.commit()
    return draft


# ---------------------------------------------------------------- quota


def _utc_day() -> str:
    return utcnow().date().isoformat()


def _reserve_quota(db, wallet_id: str, amount: int) -> None:
    """Atomically check the daily allowance and reserve `amount` wei."""
    day = _utc_day()
    try:
        with db.begin_nested():
            db.add(
                DailyUsage(
                    wallet_id=wallet_id,
                    day=day,
                    used_wei=0,
                    limit_wei=settings.daily_limit_wei,
                )
            )
            db.flush()
    except IntegrityError:
        pass  # row already exists for today

    result = db.execute(
        update(DailyUsage)
        .where(
            DailyUsage.wallet_id == wallet_id,
            DailyUsage.day == day,
            DailyUsage.used_wei + amount <= DailyUsage.limit_wei,
        )
        .values(used_wei=DailyUsage.used_wei + amount)
    )
    if result.rowcount != 1:
        raise ApiError(429, "daily quota exceeded")


def _release_quota(db, req: SignRequest) -> None:
    """Idempotently return a failed request's reservation to the daily budget."""
    if req.quota_released:
        return
    day = req.created_at.date().isoformat()
    db.execute(
        update(DailyUsage)
        .where(
            DailyUsage.wallet_id == req.wallet_id,
            DailyUsage.day == day,
            DailyUsage.used_wei >= req.quota_reserved_wei,
        )
        .values(used_wei=DailyUsage.used_wei - req.quota_reserved_wei)
    )
    req.quota_released = True
    audit(db, req.user_id, req.wallet_id, "quota_released", None, "ok", f"request={req.id}")


# ---------------------------------------------------------------- signing


def request_signature(db, user: User, draft_id: str, idempotency_key: str) -> SignRequest:
    draft = get_owned_draft(db, user, draft_id)
    if draft.status != "submitted":
        raise ApiError(409, "draft must be submitted (frozen) before signing")
    req_hash = draft.content_hash

    existing = (
        db.query(SignRequest)
        .filter_by(user_id=user.id, idempotency_key=idempotency_key)
        .one_or_none()
    )
    if existing is not None:
        if existing.request_hash != req_hash:
            raise ApiError(409, "idempotency key already used with different content")
        if existing.status == "pending":
            # Crash recovery: resume the interrupted request. Signing is
            # deterministic, so this reproduces the exact same payload.
            return _complete_signing(db, existing)
        # succeeded / failed: replay the stored result (covers lost responses)
        return existing

    wallet = get_owned_wallet(db, user, draft.wallet_id)
    if wallet.kind != "custodial":
        raise ApiError(403, "watch-only wallets cannot sign")

    # Serialize concurrent signers of this wallet (FOR UPDATE on PostgreSQL).
    db.execute(select(Wallet.id).where(Wallet.id == wallet.id).with_for_update())

    clash = (
        db.query(SignRequest)
        .filter(
            SignRequest.wallet_id == wallet.id,
            SignRequest.chain_id == draft.chain_id,
            SignRequest.nonce == draft.nonce,
            SignRequest.status.in_(["pending", "succeeded"]),
        )
        .first()
    )
    if clash is not None:
        if clash.request_hash == req_hash:
            return clash
        raise ApiError(409, "nonce already signed for a different transaction on this wallet/chain")

    now = utcnow()
    if wallet.last_signed_at is not None:
        elapsed = (now - wallet.last_signed_at).total_seconds()
        if elapsed < settings.cooldown_seconds:
            raise ApiError(429, "wallet cooldown active")

    amount = int(draft.value_wei) + int(draft.gas_limit) * int(draft.gas_price_wei)
    _reserve_quota(db, wallet.id, amount)

    req = SignRequest(
        id=str(uuid4()),
        user_id=user.id,
        wallet_id=wallet.id,
        draft_id=draft.id,
        idempotency_key=idempotency_key,
        request_hash=req_hash,
        chain_id=draft.chain_id,
        nonce=draft.nonce,
        status="pending",
        quota_reserved_wei=amount,
        quota_released=False,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(req)
    try:
        # Reservation + pending row commit atomically.
        db.commit()
    except IntegrityError:
        # Lost a race on the idempotency key; adopt the winner's row.
        db.rollback()
        winner = (
            db.query(SignRequest)
            .filter_by(user_id=user.id, idempotency_key=idempotency_key)
            .one()
        )
        if winner.request_hash != req_hash:
            raise ApiError(409, "idempotency key already used with different content")
        return _complete_signing(db, winner) if winner.status == "pending" else winner

    audit(db, user.id, wallet.id, "sign_reserved", None, "ok", f"request={req.id}")
    db.commit()
    return _complete_signing(db, req)


def _complete_signing(db, req: SignRequest) -> SignRequest:
    wallet = db.get(Wallet, req.wallet_id)
    draft = db.get(Draft, req.draft_id)
    try:
        priv = decrypt_secret(wallet.encrypted_key, aad=wallet.id.encode())
        raw, tx_hash = sign_evm_transaction(
            priv.decode(),
            chain_id=draft.chain_id,
            to=draft.to_address,
            value_wei=int(draft.value_wei),
            gas_limit=draft.gas_limit,
            gas_price_wei=int(draft.gas_price_wei),
            nonce=draft.nonce,
        )
    except Exception:
        # Deliberately generic: never leak key material or backend detail.
        req.status = "failed"
        req.error = "signing failed"
        req.updated_at = utcnow()
        _release_quota(db, req)
        audit(db, req.user_id, wallet.id, "sign_failed", None, "error", f"request={req.id}")
        db.commit()
        raise ApiError(500, "signing failed")

    req.status = "succeeded"
    req.signed_tx = raw
    req.tx_hash = tx_hash
    req.updated_at = utcnow()
    wallet.last_signed_at = utcnow()
    audit(db, req.user_id, wallet.id, "sign_succeeded", tx_hash, "ok", f"request={req.id}")
    db.commit()
    return req


def get_owned_sign_request(db, user: User, request_id: str) -> SignRequest:
    req = db.get(SignRequest, request_id)
    if req is None or req.user_id != user.id:
        raise ApiError(404, "sign request not found")
    return req
