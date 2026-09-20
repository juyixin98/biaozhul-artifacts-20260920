"""同意事件的核心写入/查询逻辑。

不变量：
- consent_events 是不可变历史，只有全部校验通过的事件才会落库（失败即整体回滚）。
- 每个 (org, subject, purpose) 的 consent_states.version 单调递增，写入必须携带
  匹配的 expected_version，否则 409。
- event_id 在组织内幂等：同 ID 同内容返回首次结果；同 ID 不同内容返回 409。
"""


import hashlib
import json
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ..models import (
    AuditLog,
    ConsentEvent,
    ConsentState,
    PolicyVersion,
    Purpose,
    Subject,
    SubjectExport,
    User,
    utcnow,
)


class ServiceError(Exception):
    status_code = 400

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class ConflictError(ServiceError):
    status_code = 409


class NotFoundError(ServiceError):
    status_code = 404


class ValidationError(ServiceError):
    status_code = 422


def _request_hash(
    *,
    event_type: str,
    subject_ref: str,
    purpose_code: str,
    policy_version_id,
    expected_version: int,
    expires_at,
) -> str:
    payload = {
        "event_type": event_type,
        "subject_ref": subject_ref,
        "purpose_code": purpose_code,
        "policy_version_id": str(policy_version_id) if policy_version_id else None,
        "expected_version": expected_version,
        "expires_at": expires_at.isoformat() if expires_at else None,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _audit(db: Session, *, org_id, actor: User, action: str, resource_type: str,
           resource_id: str, detail: dict) -> None:
    # 审计只记录不含个人字段的信息：绝不写入 subject_ref 等可识别内容。
    db.add(AuditLog(
        org_id=org_id,
        actor_id=actor.id,
        actor_role=actor.role,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        detail=detail,
    ))


def _get_purpose(db: Session, org_id, purpose_code: str) -> Purpose:
    purpose = db.scalar(
        select(Purpose).where(Purpose.org_id == org_id, Purpose.code == purpose_code)
    )
    if purpose is None:
        raise NotFoundError(f"purpose '{purpose_code}' not found")
    return purpose


def _get_or_create_subject(db: Session, org_id, subject_ref: str) -> Subject:
    # 并发下可能同时创建同一主体：用 ON CONFLICT 串行化，再读取已提交的行。
    db.execute(
        pg_insert(Subject)
        .values(org_id=org_id, subject_ref=subject_ref)
        .on_conflict_do_nothing(constraint="uq_subject_org_ref")
    )
    return db.scalar(
        select(Subject).where(Subject.org_id == org_id, Subject.subject_ref == subject_ref)
    )


def _lock_state(db: Session, org_id, subject_id, purpose_id) -> ConsentState:
    db.execute(
        pg_insert(ConsentState)
        .values(org_id=org_id, subject_id=subject_id, purpose_id=purpose_id,
                version=0, status="none")
        .on_conflict_do_nothing()
    )
    return db.scalar(
        select(ConsentState)
        .where(ConsentState.org_id == org_id,
               ConsentState.subject_id == subject_id,
               ConsentState.purpose_id == purpose_id)
        .with_for_update()
    )


def _event_result(event: ConsentEvent, status: str, replayed: bool) -> dict:
    return {
        "event_id": event.event_id,
        "seq": event.seq,
        "event_type": event.event_type,
        "resulting_version": event.resulting_version,
        "status": status,
        "replayed": replayed,
        "recorded_at": event.created_at.isoformat(),
    }


def apply_event(
    db: Session,
    *,
    org_id: UUID,
    actor: User,
    event_id: str,
    event_type: str,
    subject_ref: str,
    purpose_code: str,
    policy_version_id,
    expected_version: int,
    expires_at,
) -> dict:
    """应用一条同意事件。调用方负责 commit；任何异常都会导致整体回滚。"""
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    purpose = _get_purpose(db, org_id, purpose_code)
    subject = _get_or_create_subject(db, org_id, subject_ref)

    req_hash = _request_hash(
        event_type=event_type, subject_ref=subject_ref, purpose_code=purpose_code,
        policy_version_id=policy_version_id, expected_version=expected_version,
        expires_at=expires_at,
    )

    # 幂等：同 event_id 已存在 -> 内容一致返回原结果，不一致返回冲突。
    existing = db.scalar(
        select(ConsentEvent).where(ConsentEvent.org_id == org_id,
                                   ConsentEvent.event_id == event_id)
    )
    if existing is not None:
        if existing.request_hash != req_hash:
            raise ConflictError(
                f"event_id '{event_id}' already exists with different content"
            )
        status = "granted" if existing.event_type == "grant" else "withdrawn"
        return _event_result(existing, status, replayed=True)

    state = _lock_state(db, org_id, subject.id, purpose.id)

    if state.version != expected_version:
        raise ConflictError(
            f"expected_version {expected_version} does not match current version {state.version}"
        )

    if event_type == "grant":
        if policy_version_id is None:
            raise ValidationError("grant requires policy_version_id")
        pv = db.get(PolicyVersion, policy_version_id)
        if pv is None or pv.org_id != org_id or pv.purpose_id != purpose.id:
            raise ValidationError("policy version does not belong to this purpose")
        if pv.status != "published":
            raise ValidationError("policy version is not published")
        if expires_at is not None and expires_at <= utcnow():
            raise ValidationError("expires_at must be in the future")
        new_status = "granted"
        new_policy_id = policy_version_id
        new_expires = expires_at
    else:  # withdraw
        new_status = "withdrawn"
        new_policy_id = state.policy_version_id  # 保留依据版本供追溯
        new_expires = None

    event = ConsentEvent(
        org_id=org_id,
        subject_id=subject.id,
        purpose_id=purpose.id,
        event_id=event_id,
        event_type=event_type,
        policy_version_id=policy_version_id,
        expected_version=expected_version,
        resulting_version=state.version + 1,
        expires_at=expires_at,
        request_hash=req_hash,
    )
    db.add(event)
    db.flush()  # 分配 seq；此处失败则整个事务回滚，事件不会进入历史

    state.version += 1
    state.status = new_status
    state.last_event_seq = event.seq
    state.policy_version_id = new_policy_id
    state.expires_at = new_expires

    _audit(
        db, org_id=org_id, actor=actor,
        action=f"consent.{event_type}",
        resource_type="consent",
        resource_id=str(subject.id),
        detail={
            "purpose_code": purpose_code,
            "event_id": event_id,
            "resulting_version": state.version,
        },
    )
    return _event_result(event, new_status, replayed=False)


def verify_consent(db: Session, *, org_id, subject_ref: str, purpose_code: str) -> dict:
    """当前有效性实时计算：到期立即失效，不依赖清理任务。"""
    purpose = _get_purpose(db, org_id, purpose_code)
    subject = db.scalar(
        select(Subject).where(Subject.org_id == org_id, Subject.subject_ref == subject_ref)
    )
    if subject is None:
        raise NotFoundError("subject not found")

    state = db.scalar(
        select(ConsentState).where(
            ConsentState.org_id == org_id,
            ConsentState.subject_id == subject.id,
            ConsentState.purpose_id == purpose.id,
        )
    )
    now = utcnow()
    latest_published = db.scalar(
        select(func.max(PolicyVersion.version)).where(
            PolicyVersion.purpose_id == purpose.id,
            PolicyVersion.status == "published",
        )
    )

    base = {
        "subject_ref": subject_ref,
        "purpose_code": purpose_code,
        "latest_policy_version": latest_published,
        "evaluated_at": now.isoformat(),
    }
    if state is None or state.status == "none":
        return {**base, "valid": False, "status": "none", "state_version": 0,
                "basis_event": None, "policy_version": None,
                "policy_stale": False, "expires_at": None, "expired": False}

    expired = state.expires_at is not None and state.expires_at <= now
    valid = state.status == "granted" and not expired

    basis = db.scalar(select(ConsentEvent).where(ConsentEvent.seq == state.last_event_seq))
    policy_info = None
    policy_stale = False
    if state.policy_version_id is not None:
        pv = db.get(PolicyVersion, state.policy_version_id)
        if pv is not None:
            policy_info = {"id": str(pv.id), "version": pv.version}
            policy_stale = latest_published is not None and pv.version < latest_published

    return {
        **base,
        "valid": valid,
        "status": state.status,
        "state_version": state.version,
        "basis_event": {
            "event_id": basis.event_id,
            "event_type": basis.event_type,
            "seq": basis.seq,
            "recorded_at": basis.created_at.isoformat(),
        } if basis else None,
        "policy_version": policy_info,
        "policy_stale": policy_stale,
        "expires_at": state.expires_at.isoformat() if state.expires_at else None,
        "expired": expired,
    }


def event_history(db: Session, *, org_id, subject_ref: str, purpose_code: str) -> list[dict]:
    purpose = _get_purpose(db, org_id, purpose_code)
    subject = db.scalar(
        select(Subject).where(Subject.org_id == org_id, Subject.subject_ref == subject_ref)
    )
    if subject is None:
        raise NotFoundError("subject not found")
    events = db.scalars(
        select(ConsentEvent)
        .where(ConsentEvent.org_id == org_id,
               ConsentEvent.subject_id == subject.id,
               ConsentEvent.purpose_id == purpose.id)
        .order_by(ConsentEvent.seq)
    ).all()
    return [
        {
            "seq": e.seq,
            "event_id": e.event_id,
            "event_type": e.event_type,
            "policy_version_id": str(e.policy_version_id) if e.policy_version_id else None,
            "expected_version": e.expected_version,
            "resulting_version": e.resulting_version,
            "expires_at": e.expires_at.isoformat() if e.expires_at else None,
            "recorded_at": e.created_at.isoformat(),
        }
        for e in events
    ]


def _fold(st: dict, e: ConsentEvent) -> dict:
    """把一条事件折叠进状态。增量写入与全量重建共用同一语义。"""
    st["version"] += 1
    st["last_event_seq"] = e.seq
    if e.event_type == "grant":
        st["status"] = "granted"
        st["policy_version_id"] = e.policy_version_id
        st["expires_at"] = e.expires_at
    else:
        st["status"] = "withdrawn"
        st["expires_at"] = None
    return st


def rebuild_states(db: Session, *, org_id, actor: User) -> dict:
    """从不可变事件历史重建当前状态。

    并发安全：重建期间新写入的事件会把 state.version 推得更高，
    条件 upsert (version < excluded.version) 保证重建结果不会覆盖更新的状态，
    事件本身在 consent_events 中永不丢失。
    """
    events = db.scalars(
        select(ConsentEvent).where(ConsentEvent.org_id == org_id).order_by(ConsentEvent.seq)
    ).all()

    states: dict[tuple, dict] = {}
    for e in events:
        key = (e.subject_id, e.purpose_id)
        st = states.setdefault(key, {"version": 0, "status": "none",
                                     "last_event_seq": None,
                                     "policy_version_id": None, "expires_at": None})
        _fold(st, e)

    for (subject_id, purpose_id), st in states.items():
        stmt = pg_insert(ConsentState).values(
            org_id=org_id, subject_id=subject_id, purpose_id=purpose_id,
            version=st["version"], status=st["status"],
            last_event_seq=st["last_event_seq"],
            policy_version_id=st["policy_version_id"], expires_at=st["expires_at"],
        )
        excluded = stmt.excluded
        stmt = stmt.on_conflict_do_update(
            index_elements=["org_id", "subject_id", "purpose_id"],
            set_={
                "version": excluded.version,
                "status": excluded.status,
                "last_event_seq": excluded.last_event_seq,
                "policy_version_id": excluded.policy_version_id,
                "expires_at": excluded.expires_at,
                "updated_at": utcnow(),
            },
            where=ConsentState.version < excluded.version,
        )
        db.execute(stmt)

    _audit(db, org_id=org_id, actor=actor, action="consent.rebuild",
           resource_type="consent_states", resource_id="*",
           detail={"events": len(events), "states": len(states)})
    return {"events_replayed": len(events), "states_rebuilt": len(states)}


def import_batch(db: Session, *, org_id, actor: User, items: list[dict]) -> list[dict]:
    """批量导入：同一事务内逐条应用，任何错误 -> 整批回滚（最多 500 条由 schema 限制）。"""
    results = [
        apply_event(db, org_id=org_id, actor=actor, **item) for item in items
    ]
    _audit(db, org_id=org_id, actor=actor, action="consent.batch_import",
           resource_type="consent", resource_id="*",
           detail={"count": len(items)})
    return results


def create_export(db: Session, *, org_id, actor: User, subject_ref: str) -> dict:
    purpose_rows = db.scalars(select(Purpose).where(Purpose.org_id == org_id)).all()
    purposes_by_id = {p.id: p for p in purpose_rows}
    subject = db.scalar(
        select(Subject).where(Subject.org_id == org_id, Subject.subject_ref == subject_ref)
    )
    if subject is None:
        raise NotFoundError("subject not found")

    states = db.scalars(
        select(ConsentState).where(ConsentState.org_id == org_id,
                                   ConsentState.subject_id == subject.id)
    ).all()
    now = utcnow()
    consents = []
    for s in states:
        purpose = purposes_by_id.get(s.purpose_id)
        pv = db.get(PolicyVersion, s.policy_version_id) if s.policy_version_id else None
        expired = s.expires_at is not None and s.expires_at <= now
        consents.append({
            "purpose_code": purpose.code if purpose else str(s.purpose_id),
            "status": s.status,
            "valid": s.status == "granted" and not expired,
            "state_version": s.version,
            "policy_version": pv.version if pv else None,
            "expires_at": s.expires_at.isoformat() if s.expires_at else None,
        })

    export = SubjectExport(
        org_id=org_id,
        subject_id=subject.id,
        created_by=actor.id,
        payload={
            "subject_ref": subject_ref,
            "generated_at": now.isoformat(),
            "consents": consents,
        },
    )
    db.add(export)
    db.flush()
    _audit(db, org_id=org_id, actor=actor, action="subject.exported",
           resource_type="subject", resource_id=str(subject.id),
           detail={"export_id": str(export.id)})
    return {"export_id": str(export.id), "payload": export.payload}


def list_exports(db: Session, *, org_id, subject_ref: str) -> list[dict]:
    subject = db.scalar(
        select(Subject).where(Subject.org_id == org_id, Subject.subject_ref == subject_ref)
    )
    if subject is None:
        raise NotFoundError("subject not found")
    rows = db.scalars(
        select(SubjectExport)
        .where(SubjectExport.org_id == org_id, SubjectExport.subject_id == subject.id)
        .order_by(SubjectExport.created_at)
    ).all()
    return [{"export_id": str(r.id), "created_at": r.created_at.isoformat(),
             "payload": r.payload} for r in rows]


def delete_subject(db: Session, *, org_id, actor: User, subject_ref: str) -> dict:
    """删除主体：清除可识别映射与全部导出副本；审计只留不含个人字段的记录。"""
    subject = db.scalar(
        select(Subject).where(Subject.org_id == org_id, Subject.subject_ref == subject_ref)
    )
    if subject is None:
        raise NotFoundError("subject not found")
    subject_id = subject.id
    removed = db.execute(
        delete(SubjectExport).where(SubjectExport.org_id == org_id,
                                    SubjectExport.subject_id == subject_id)
    ).rowcount
    db.delete(subject)
    _audit(db, org_id=org_id, actor=actor, action="subject.deleted",
           resource_type="subject", resource_id=str(subject_id),
           detail={"exports_removed": removed})
    return {"deleted": True, "exports_removed": removed}
