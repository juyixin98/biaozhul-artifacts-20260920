from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import func, update
from sqlalchemy.orm import Session

from ..models import Alert, AlertInvestigation, AlertStatus

# Allowed triage transitions. Any state may move to any non-open state;
# open is the initial state and cannot be returned to.
_ALLOWED_TARGETS = {AlertStatus.confirmed, AlertStatus.false_positive, AlertStatus.investigated}


def triage_alert(
    db: Session,
    *,
    alert: Alert,
    actor_id: int,
    new_status: AlertStatus,
    expected_version: int,
    note: str,
) -> Alert:
    if new_status is AlertStatus.open:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Alerts cannot be moved back to open",
        )
    if new_status not in _ALLOWED_TARGETS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported status")

    # Atomic compare-and-set on the version token. Concurrent triage: exactly
    # one transaction wins; the loser gets 409 and must refetch.
    result = db.execute(
        update(Alert)
        .where(Alert.id == alert.id, Alert.version == expected_version)
        .values(
            status=new_status.value,
            version=expected_version + 1,
            updated_at=func.now(),
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.rollback()
        current = db.get(Alert, alert.id)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "version_conflict",
                "submitted_version": expected_version,
                "current_version": current.version if current else None,
                "current_status": current.status.value if current else None,
            },
        )

    # Immutable audit record: recomputation / retriage never alters these.
    db.add(
        AlertInvestigation(
            alert_id=alert.id,
            actor_id=actor_id,
            action=new_status,
            note=note,
            from_version=expected_version,
            to_version=expected_version + 1,
            evidence_snapshot=alert.evidence or {},
        )
    )
    db.flush()
    db.refresh(alert)
    return alert
