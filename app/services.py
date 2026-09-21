"""Signing orchestration: idempotency, nonce exclusivity, daily quota and
cooldown - all enforced atomically inside one database transaction.

Quota / recovery contract (see README for the full description):

* A pending or success SignRequest *occupies* `value_wei` of its wallet's
  allowance for the UTC day recorded in `quota_day`.
* Check-and-occupy is a single SQL transaction that takes a row lock on the
  wallet (SELECT ... FOR UPDATE), so concurrent signers serialize there and can
  never both pass the allowance check.
* Signing is two-phase: PHASE 1 (single transaction) checks quota/cooldown/
  nonce and commits the `pending` occupation + frozen draft; PHASE 2 performs
  the local CPU signing and commits `success` or `failed` on the SAME row.
* A crash before the PHASE-1 commit rolls the occupation back (never debited).
  A crash between the two commits leaves a durable `pending` row carrying the
  hold; recovery is "resume" (finish the same row) or "release" (return it).
* Retrying with the same idempotency key returns the same result (or resumes a
  pending attempt) - no second result, no second debit.
* `failed` rows and explicitly `released` pending rows get quota_day = NULL and
  free the nonce, so they no longer count toward allowance or nonce exclusivity.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import audit
from app.config import settings
from app.crypto import decrypt_private_key, encrypt_private_key
from app.errors import BadRequest, ConflictError, Forbidden, LimitError, NotFound
from app.models import (
    DRAFT_FROZEN,
    DRAFT_OPEN,
    REQ_FAILED,
    REQ_PENDING,
    REQ_RELEASED,
    REQ_SUCCESS,
    Draft,
    SignRequest,
    Wallet,
)
from app.signing import TxContent, _decode_data, address_from_hex, sign_legacy_tx

logger = logging.getLogger("vaultcommand")


# ----------------------------------------------------------------- helpers ----

def utc_day(now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now(dt.timezone.utc)
    return now.astimezone(dt.timezone.utc).date().isoformat()


def canonical_content(
    *,
    wallet_id: str,
    chain_id: int,
    to_address: str,
    value_wei: int,
    gas: int,
    gas_price_wei: int,
    nonce: int,
    data_hex: str,
) -> str:
    """Stable fingerprint of the signed content. The wallet id is part of it so
    the same content submitted against another wallet cannot collide."""
    payload = json.dumps(
        {
            "wallet_id": wallet_id,
            "chain_id": int(chain_id),
            "to_address": to_address,
            "value_wei": str(int(value_wei)),
            "gas": int(gas),
            "gas_price_wei": str(int(gas_price_wei)),
            "nonce": int(nonce),
            "data_hex": (data_hex or "0x").lower(),
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _tx_summary(req: SignRequest, *, include_hash: bool = False) -> dict[str, Any]:
    """Only safe, non-secret transaction summary fields."""
    s: dict[str, Any] = {
        "chain_id": int(req.chain_id),
        "nonce": int(req.nonce),
        "to": req.to_address,
        "value_wei": str(int(req.value_wei)),
    }
    if include_hash and req.tx_hash:
        s["tx_hash"] = req.tx_hash
    return s


def _lock_wallet(db: Session, *, user_id: str, wallet_id: str) -> Wallet:
    """Fetch the user's wallet with FOR UPDATE (serializes per-wallet traffic)
    or raise 404 - existence is never revealed across users."""
    wallet = db.scalar(
        select(Wallet).where(Wallet.id == wallet_id).with_for_update()
    )
    if wallet is None or wallet.user_id != user_id:
        raise NotFound("wallet_not_found", "wallet not found")
    return wallet


def _get_owned_wallet(db: Session, *, user_id: str, wallet_id: str) -> Wallet:
    wallet = db.get(Wallet, wallet_id)
    if wallet is None or wallet.user_id != user_id:
        raise NotFound("wallet_not_found", "wallet not found")
    return wallet


# ---------------------------------------------------------------- wallets ----

def create_wallet(
    db: Session,
    *,
    user_id: str,
    label: str,
    kind: str,
    private_key_hex: str | None,
    address: str | None,
    request_id: str | None,
) -> Wallet:
    from app.models import WALLET_CUSTODIAL, WALLET_WATCH_ONLY

    if kind == WALLET_CUSTODIAL:
        if not private_key_hex:
            raise ConflictError(
                "private_key_required", "custodial wallet requires private_key_hex"
            )
        # Validate the key and derive its address BEFORE persisting anything.
        try:
            derived = address_from_hex(private_key_hex)
        except (ValueError, Exception):  # noqa: BLE001 - never echo key material
            raise ConflictError("invalid_private_key", "invalid private key")
        blob = encrypt_private_key(private_key_hex)
        wallet = Wallet(
            id=str(uuid.uuid4()),
            user_id=user_id,
            label=label,
            kind=kind,
            address=derived,
            key_ciphertext=blob,
        )
    else:
        if not address:
            raise ConflictError(
                "address_required", "watch_only wallet requires address"
            )
        wallet = Wallet(
            id=str(uuid.uuid4()),
            user_id=user_id,
            label=label,
            kind=WALLET_WATCH_ONLY,
            address=address,
            key_ciphertext=None,
        )
    db.add(wallet)
    db.flush()
    audit.record(
        db,
        user_id=user_id,
        action="wallet.create",
        result=audit.OK,
        request_id=request_id,
        wallet_id=wallet.id,
        summary={"kind": kind, "address": wallet.address},
    )
    db.commit()
    db.refresh(wallet)
    return wallet


# ---------------------------------------------------------------- drafts -----

def create_draft(db: Session, *, user_id: str, content: dict, request_id) -> Draft:
    _get_owned_wallet(db, user_id=user_id, wallet_id=content["wallet_id"])
    draft = Draft(
        id=str(uuid.uuid4()), user_id=user_id, status=DRAFT_OPEN, **content
    )
    db.add(draft)
    db.flush()
    audit.record(
        db,
        user_id=user_id,
        action="draft.create",
        result=audit.OK,
        request_id=request_id,
        wallet_id=draft.wallet_id,
        sign_request_id=None,
    )
    db.commit()
    db.refresh(draft)
    return draft


def update_draft(db: Session, *, user_id: str, draft_id: str, patch: dict) -> Draft:
    draft = db.get(Draft, draft_id)
    if draft is None or draft.user_id != user_id:
        raise NotFound("draft_not_found", "draft not found")
    if draft.status != DRAFT_OPEN:
        raise ConflictError(
            "draft_frozen", "draft is frozen after submission and cannot change"
        )
    for key, value in patch.items():
        if value is not None:
            setattr(draft, key, value)
    db.commit()
    db.refresh(draft)
    return draft


def delete_draft(db: Session, *, user_id: str, draft_id: str) -> None:
    draft = db.get(Draft, draft_id)
    if draft is None or draft.user_id != user_id:
        raise NotFound("draft_not_found", "draft not found")
    if draft.status == DRAFT_FROZEN:
        raise ConflictError("draft_frozen", "frozen drafts are retained for audit")
    db.delete(draft)
    db.commit()


# --------------------------------------------------------------- signing -----

def _free_value_for_today(db: Session, wallet: Wallet, day: str) -> tuple[int, SignRequest | None]:
    """Must run while holding the wallet row lock. Returns
    (free_wei, latest_success_today)."""
    rows = db.scalars(
        select(SignRequest)
        .where(
            SignRequest.wallet_id == wallet.id,
            SignRequest.quota_day == day,
            SignRequest.status.in_([REQ_PENDING, REQ_SUCCESS]),
        )
        .order_by(SignRequest.created_at.asc())
    ).all()
    used = sum(int(r.value_wei) for r in rows)
    last_success = None
    for r in rows:
        if r.status == REQ_SUCCESS:
            last_success = r
    free = settings.daily_quota_wei - used
    return free, last_success


def _persist_success(db: Session, req: SignRequest, signed) -> None:
    req.status = REQ_SUCCESS
    req.raw_transaction_hex = signed.raw_transaction_hex
    req.tx_hash = signed.tx_hash
    req.error_reason = None
    req.signed_at = dt.datetime.now(dt.timezone.utc)
    req.finished_at = req.signed_at


def submit_sign(
    db: Session,
    *,
    user_id: str,
    draft_id: str,
    idempotency_key: str,
    request_id: str | None,
) -> tuple[SignRequest, bool]:
    """Submit/resume a signature. Returns (request, replayed).

    Raises on rule violations; every raised path either rolled the transaction
    back or only recorded the rejection (quota never occupied for rejections)."""
    # ---- idempotent lookup first (no lock needed for the common retry) ----
    existing = db.scalar(
        select(SignRequest).where(
            SignRequest.user_id == user_id,
            SignRequest.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        draft = db.get(Draft, draft_id)
        if draft is None or draft.user_id != user_id:
            raise NotFound("draft_not_found", "draft not found")
        # Same key + different content -> conflict. Compare the draft's frozen
        # content to the request's stored content hash.
        current_hash = canonical_content(
            wallet_id=existing.wallet_id,
            chain_id=draft.chain_id,
            to_address=draft.to_address,
            value_wei=int(draft.value_wei),
            gas=draft.gas,
            gas_price_wei=int(draft.gas_price_wei),
            nonce=draft.nonce,
            data_hex=draft.data_hex or "0x",
        )
        if existing.content_hash != current_hash or existing.draft_id != draft_id:
            raise ConflictError(
                "idempotency_conflict",
                "idempotency key already used with different content",
            )
        if existing.status == REQ_PENDING:
            # Process interrupted mid-flight: resume using the SAME occupied row.
            return _complete_pending(db, existing, request_id, via="submit"), True
        return existing, True

    # ---- new request: load draft + lock wallet in one transaction ----
    draft = db.get(Draft, draft_id)
    if draft is None or draft.user_id != user_id:
        raise NotFound("draft_not_found", "draft not found")

    wallet = _lock_wallet(db, user_id=user_id, wallet_id=draft.wallet_id)

    from app.models import WALLET_CUSTODIAL

    if wallet.kind != WALLET_CUSTODIAL or wallet.key_ciphertext is None:
        # Watch-only wallets cannot sign. No occupation is created.
        audit.record(
            db,
            user_id=user_id,
            action="sign.submit",
            result=audit.DENIED,
            request_id=request_id,
            wallet_id=wallet.id,
            summary={"chain_id": int(draft.chain_id), "nonce": int(draft.nonce)},
            detail="watch_only wallet cannot sign",
        )
        db.commit()
        raise Forbidden(
            "watch_only_cannot_sign", "watch-only wallets cannot sign transactions"
        )

    if draft.status == DRAFT_FROZEN:
        # A frozen draft whose request still occupies its nonce is bound to it.
        # Requests that failed/were released no longer occupy anything, so the
        # draft may be signed again under a new idempotency key.
        bound = db.scalar(
            select(SignRequest)
            .where(SignRequest.draft_id == draft.id)
            .order_by(SignRequest.created_at.desc())
            .limit(1)
        )
        if bound is not None and bound.status in (REQ_PENDING, REQ_SUCCESS):
            raise ConflictError(
                "draft_already_submitted",
                "draft already submitted; reuse its idempotency key",
            )

    content_hash = canonical_content(
        wallet_id=wallet.id,
        chain_id=draft.chain_id,
        to_address=draft.to_address,
        value_wei=int(draft.value_wei),
        gas=draft.gas,
        gas_price_wei=int(draft.gas_price_wei),
        nonce=draft.nonce,
        data_hex=draft.data_hex or "0x",
    )

    # ---- same wallet+chain+nonce cannot sign a DIFFERENT transaction ----
    occupant = db.scalar(
        select(SignRequest)
        .where(
            SignRequest.wallet_id == wallet.id,
            SignRequest.chain_id == draft.chain_id,
            SignRequest.nonce == draft.nonce,
            SignRequest.status.in_([REQ_PENDING, REQ_SUCCESS]),
        )
        .order_by(SignRequest.created_at.desc())
        .limit(1)
    )
    if occupant is not None:
        if occupant.content_hash != content_hash:
            audit.record(
                db,
                user_id=user_id,
                action="sign.submit",
                result=audit.DENIED,
                request_id=request_id,
                wallet_id=wallet.id,
                summary={
                    "chain_id": int(draft.chain_id),
                    "nonce": int(draft.nonce),
                    "existing_request_id": occupant.id,
                },
                detail="nonce already used with different transaction content",
            )
            db.commit()
            raise ConflictError(
                "nonce_conflict",
                "wallet+chain_id+nonce is already occupied by a different transaction",
            )
        # Identical content under a new idempotency key is a replay of the same
        # logical transaction - return the existing result, sign nothing new.
        if occupant.status == REQ_PENDING:
            return _complete_pending(db, occupant, request_id, via="replay"), True
        return occupant, True

    day = utc_day()
    free_wei, last_success = _free_value_for_today(db, wallet, day)

    # ---- cooldown ----
    now = dt.datetime.now(dt.timezone.utc)
    if settings.cooldown_seconds > 0 and last_success is not None:
        elapsed = (now - last_success.signed_at).total_seconds()
        if elapsed < settings.cooldown_seconds:
            audit.record(
                db,
                user_id=user_id,
                action="sign.submit",
                result=audit.DENIED,
                request_id=request_id,
                wallet_id=wallet.id,
                summary={"chain_id": int(draft.chain_id), "nonce": int(draft.nonce)},
                detail=f"cooldown active, retry after {settings.cooldown_seconds - elapsed:.0f}s",
            )
            db.commit()
            raise LimitError(
                "cooldown_active",
                f"wallet cooldown active; retry in "
                f"{max(1, int(settings.cooldown_seconds - elapsed))} seconds",
            )

    # ---- atomic daily quota check ----
    if int(draft.value_wei) > free_wei:
        audit.record(
            db,
            user_id=user_id,
            action="sign.submit",
            result=audit.DENIED,
            request_id=request_id,
            wallet_id=wallet.id,
            summary={
                "chain_id": int(draft.chain_id),
                "nonce": int(draft.nonce),
                "requested_wei": str(int(draft.value_wei)),
                "free_wei": str(free_wei),
            },
            detail="daily quota exceeded",
        )
        db.commit()
        raise LimitError(
            "daily_quota_exceeded",
            f"daily quota exceeded: requested {int(draft.value_wei)} wei, "
            f"{free_wei} wei remaining today",
        )

    # ---- PHASE 1 (one transaction): atomically check quota/cooldown/nonce
    # and persist the pending occupation. After this COMMIT the quota hold is
    # durable even if the process is killed during local signing; recovery is
    # then "resume" (finish the same row) or "release" (give the hold back).
    req = SignRequest(
        id=str(uuid.uuid4()),
        user_id=user_id,
        wallet_id=wallet.id,
        draft_id=draft.id,
        idempotency_key=idempotency_key,
        content_hash=content_hash,
        chain_id=draft.chain_id,
        to_address=draft.to_address,
        value_wei=int(draft.value_wei),
        gas=int(draft.gas),
        gas_price_wei=int(draft.gas_price_wei),
        nonce=int(draft.nonce),
        data_hex=draft.data_hex or "0x",
        status=REQ_PENDING,
        quota_day=day,
    )
    db.add(req)
    draft.status = DRAFT_FROZEN
    draft.frozen_at = now
    try:
        db.flush()  # unique constraints (idempotency / nonce) enforced here
        db.commit()
    except IntegrityError:
        # We held the wallet FOR UPDATE, so the loser is a concurrent request
        # using the same idempotency key. Roll back and resolve by content.
        db.rollback()
        raced = db.scalar(
            select(SignRequest).where(
                SignRequest.user_id == user_id,
                SignRequest.idempotency_key == idempotency_key,
            )
        )
        if raced is not None and raced.content_hash == content_hash:
            if raced.status == REQ_PENDING:
                return _complete_pending(db, raced, request_id, via="replay"), True
            return raced, True
        raise ConflictError(
            "idempotency_conflict",
            "idempotency key already used with different content",
        )

    # ---- PHASE 2: local signing (no chain, no broadcast). Finish the SAME
    # occupied row. A crash here leaves a durable `pending` row that recovery
    # handles; it never creates a second hold.
    return _complete_pending(db, req, request_id, via="submit"), False


def _complete_pending(
    db: Session, req: SignRequest, request_id: str | None, *, via: str
) -> SignRequest:
    """Locally sign and finalize a pending request on the row already holding
    quota and the nonce. On failure the hold is released atomically (row marked
    failed, quota_day NULL, nonce freed); on a crash the row simply stays
    pending and is resumed/released later - never double-debited."""
    # Reload under a fresh lock; if another worker already finished it, return
    # that result instead of signing again.
    wallet = _lock_wallet(db, user_id=req.user_id, wallet_id=req.wallet_id)
    current = db.get(SignRequest, req.id)
    if current is None:  # pragma: no cover - defensive
        raise NotFound("sign_request_not_found", "sign request not found")
    if current.status != REQ_PENDING:
        db.rollback()
        return current

    try:
        private_key = decrypt_private_key(wallet.key_ciphertext)
    except Exception:  # noqa: BLE001 - surface a generic error, never key detail
        logger.warning("custodial key decryption failed for wallet %s", wallet.id)
        return _fail_pending(db, current, "private key could not be decrypted", request_id)
    try:
        signed = sign_legacy_tx(
            private_key,
            TxContent(
                chain_id=int(current.chain_id),
                to_address=current.to_address,
                value_wei=int(current.value_wei),
                gas=int(current.gas),
                gas_price_wei=int(current.gas_price_wei),
                nonce=int(current.nonce),
                data=_decode_data(current.data_hex),
            ),
        )
    except Exception:  # noqa: BLE001 - never propagate crypto internals
        logger.warning(
            "offline signing raised (wallet=%s nonce=%s)",
            wallet.id,
            current.nonce,
        )
        return _fail_pending(db, current, "transaction could not be signed", request_id)
    finally:
        # Drop the secret reference promptly; it never leaves this function.
        try:
            del private_key
        except UnboundLocalError:
            pass

    _persist_success(db, current, signed)
    audit.record(
        db,
        user_id=req.user_id,
        action=f"sign.{via}" if via == "submit" else f"sign.resume_{via}",
        result=audit.OK,
        request_id=request_id,
        wallet_id=wallet.id,
        sign_request_id=current.id,
        summary=_tx_summary(current, include_hash=True),
        detail=None if via == "submit" else "completed pending request",
    )
    db.commit()
    db.refresh(current)
    return current


def _fail_pending(
    db: Session, req: SignRequest, reason: str, request_id: str | None
) -> SignRequest:
    """Mark a pending request failed and RELEASE its hold atomically:
    quota_day -> NULL (no longer debited) and the partial nonce index stops
    covering the row (nonce reusable)."""
    req.status = REQ_FAILED
    req.quota_day = None
    req.error_reason = reason[:240]
    req.finished_at = dt.datetime.now(dt.timezone.utc)
    audit.record(
        db,
        user_id=req.user_id,
        action="sign.failed",
        result=audit.ERROR,
        request_id=request_id,
        wallet_id=req.wallet_id,
        sign_request_id=req.id,
        summary=_tx_summary(req),
        detail=reason[:240],
    )
    db.commit()
    db.refresh(req)
    raise BadRequest("signing_failed", reason)


def resume_sign(
    db: Session, *, user_id: str, sign_request_id: str, request_id: str | None
) -> SignRequest:
    req = db.get(SignRequest, sign_request_id)
    if req is None or req.user_id != user_id:
        raise NotFound("sign_request_not_found", "sign request not found")
    if req.status != REQ_PENDING:
        raise ConflictError(
            "not_pending", f"request is already {req.status}; nothing to resume"
        )
    return _complete_pending(db, req, request_id, via="endpoint")


def release_pending(
    db: Session, *, user_id: str, sign_request_id: str, request_id: str | None
) -> SignRequest:
    """Explicitly abandon a pending request and recover its quota/nonce hold."""
    req = db.get(SignRequest, sign_request_id)
    if req is None or req.user_id != user_id:
        raise NotFound("sign_request_not_found", "sign request not found")
    if req.status != REQ_PENDING:
        raise ConflictError(
            "not_pending", f"request is already {req.status}; cannot release"
        )
    wallet = _lock_wallet(db, user_id=user_id, wallet_id=req.wallet_id)
    req.status = REQ_RELEASED
    req.quota_day = None
    req.error_reason = "released by user"
    req.finished_at = dt.datetime.now(dt.timezone.utc)
    audit.record(
        db,
        user_id=user_id,
        action="sign.release",
        result=audit.OK,
        request_id=request_id,
        wallet_id=wallet.id,
        sign_request_id=req.id,
        summary=_tx_summary(req),
        detail="pending hold released; quota and nonce recovered",
    )
    db.commit()
    db.refresh(req)
    return req
