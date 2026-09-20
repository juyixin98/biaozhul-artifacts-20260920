"""过账服务：借贷校验、幂等键、期间锁、冲销。"""
import hashlib
import json

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import config
from app.models import (
    Account,
    AuditLog,
    Department,
    Fund,
    JournalEntry,
    JournalLine,
    Period,
)

MAX_LINE_CENTS = config.MAX_AMOUNT_CENTS


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def request_hash(period_id: int, description: str, lines: list[dict],
                 reversal_of_id: int | None) -> str:
    """请求内容的规范化哈希，用于幂等键冲突检测。"""
    canonical = {
        "period_id": period_id,
        "description": description,
        "reversal_of_id": reversal_of_id,
        "lines": sorted(
            (
                {
                    "fund_id": l["fund_id"],
                    "department_id": l["department_id"],
                    "account_id": l["account_id"],
                    "debit_cents": l["debit_cents"],
                    "credit_cents": l["credit_cents"],
                }
                for l in lines
            ),
            key=lambda l: json.dumps(l, sort_keys=True),
        ),
    }
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True).encode()
    ).hexdigest()


def validate_lines(lines: list[dict]) -> None:
    """借贷平衡与金额边界校验。任何不合法都拒绝整张凭证。"""
    if len(lines) < 2:
        raise _error(422, "TOO_FEW_LINES", "一张凭证至少需要两条分录行")
    total_debit = 0
    total_credit = 0
    for i, line in enumerate(lines):
        debit = line["debit_cents"]
        credit = line["credit_cents"]
        if debit < 0 or credit < 0:
            raise _error(422, "NEGATIVE_AMOUNT", f"第 {i + 1} 行金额不能为负")
        if debit > MAX_LINE_CENTS or credit > MAX_LINE_CENTS:
            raise _error(422, "AMOUNT_TOO_LARGE", f"第 {i + 1} 行金额超出上限")
        if debit == 0 and credit == 0:
            raise _error(422, "ZERO_LINE", f"第 {i + 1} 行借贷不能同时为零")
        if debit > 0 and credit > 0:
            raise _error(422, "DOUBLE_SIDED", f"第 {i + 1} 行借贷只能填一方")
        total_debit += debit
        total_credit += credit
    if total_debit > MAX_LINE_CENTS or total_credit > MAX_LINE_CENTS:
        raise _error(422, "TOTAL_TOO_LARGE", "凭证合计金额超出上限")
    if total_debit != total_credit:
        raise _error(
            422,
            "UNBALANCED",
            f"借贷不平衡：借方 {total_debit} 分，贷方 {total_credit} 分",
        )


def _check_refs(db: Session, lines: list[dict]) -> None:
    fund_ids = {l["fund_id"] for l in lines}
    dept_ids = {l["department_id"] for l in lines}
    acct_ids = {l["account_id"] for l in lines}
    for model, ids, label in (
        (Fund, fund_ids, "基金"),
        (Department, dept_ids, "部门"),
        (Account, acct_ids, "科目"),
    ):
        found = set(db.scalars(select(model.id).where(model.id.in_(ids))))
        missing = ids - found
        if missing:
            raise _error(422, "INVALID_REF", f"无效的{label}引用: {sorted(missing)}")


def _lock_open_period(db: Session, period_id: int) -> Period:
    """锁定期间行（与关账/重开串行化），并确认其处于开放状态。"""
    period = db.execute(
        select(Period).where(Period.id == period_id).with_for_update()
    ).scalar_one_or_none()
    if period is None:
        raise _error(404, "PERIOD_NOT_FOUND", f"期间 {period_id} 不存在")
    if period.status != "open":
        raise _error(409, "PERIOD_CLOSED", f"期间 {period.year}-{period.month:02d} 已关闭，不能写入")
    return period


def _insert_entry(
    db: Session,
    *,
    period_id: int,
    description: str,
    lines: list[dict],
    actor: str,
    idempotency_key: str | None,
    reversal_of_id: int | None,
) -> JournalEntry:
    _check_refs(db, lines)
    validate_lines(lines)
    entry = JournalEntry(
        idempotency_key=idempotency_key,
        request_hash=request_hash(period_id, description, lines, reversal_of_id),
        period_id=period_id,
        description=description,
        status="posted",
        reversal_of_id=reversal_of_id,
        created_by=actor,
    )
    entry.lines = [JournalLine(**l) for l in lines]
    db.add(entry)
    db.flush()
    return entry


def post_journal(
    db: Session,
    *,
    period_id: int,
    description: str,
    lines: list[dict],
    actor: str,
    idempotency_key: str,
) -> tuple[JournalEntry, bool]:
    """过账。返回 (凭证, 是否为幂等重放)。

    幂等语义：相同键+相同内容返回原结果；相同键+不同内容报 409。
    """
    _lock_open_period(db, period_id)
    digest = request_hash(period_id, description, lines, None)

    existing = db.scalar(
        select(JournalEntry).where(JournalEntry.idempotency_key == idempotency_key)
    )
    if existing is not None:
        if existing.request_hash == digest:
            return existing, True
        raise _error(409, "IDEMPOTENCY_CONFLICT", "幂等键已被不同内容的请求使用")

    try:
        entry = _insert_entry(
            db,
            period_id=period_id,
            description=description,
            lines=lines,
            actor=actor,
            idempotency_key=idempotency_key,
            reversal_of_id=None,
        )
        db.add(AuditLog(actor=actor, action="post_journal",
                        entity_type="journal_entry", entity_id=str(entry.id),
                        detail=f"lines={len(lines)}"))
        db.commit()
    except IntegrityError:
        # 并发下同键插入：回滚后按幂等语义重新判定
        db.rollback()
        existing = db.scalar(
            select(JournalEntry).where(JournalEntry.idempotency_key == idempotency_key)
        )
        if existing is not None and existing.request_hash == digest:
            return existing, True
        raise _error(409, "IDEMPOTENCY_CONFLICT", "幂等键已被不同内容的请求使用")
    return entry, False


def reverse_journal(
    db: Session,
    *,
    entry_id: int,
    period_id: int,
    actor: str,
    reason: str,
) -> JournalEntry:
    """在开放期间对已过账凭证追加冲销凭证。一张凭证只能被冲销一次。"""
    _lock_open_period(db, period_id)
    original = db.get(JournalEntry, entry_id)
    if original is None:
        raise _error(404, "ENTRY_NOT_FOUND", f"凭证 {entry_id} 不存在")
    already = db.scalar(
        select(JournalEntry).where(JournalEntry.reversal_of_id == entry_id)
    )
    if already is not None:
        raise _error(409, "ALREADY_REVERSED", f"凭证 {entry_id} 已被冲销（冲销凭证 {already.id}）")

    lines = [
        {
            "fund_id": l.fund_id,
            "department_id": l.department_id,
            "account_id": l.account_id,
            "debit_cents": l.credit_cents,
            "credit_cents": l.debit_cents,
        }
        for l in original.lines
    ]
    description = f"冲销凭证 #{entry_id}: {reason}"
    try:
        entry = _insert_entry(
            db,
            period_id=period_id,
            description=description,
            lines=lines,
            actor=actor,
            idempotency_key=None,
            reversal_of_id=entry_id,
        )
        db.add(AuditLog(actor=actor, action="reverse_journal",
                        entity_type="journal_entry", entity_id=str(entry.id),
                        reason=reason, detail=f"reversal_of={entry_id}"))
        db.commit()
    except IntegrityError:
        # 并发冲销同一凭证：唯一约束兜底
        db.rollback()
        raise _error(409, "ALREADY_REVERSED", f"凭证 {entry_id} 已被冲销")
    return entry
