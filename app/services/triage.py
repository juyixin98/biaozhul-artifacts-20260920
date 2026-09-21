"""Alert triage with optimistic locking."""
from sqlalchemy import func, update
from sqlalchemy.orm import Session

from app.models import Alert


class VersionConflictError(Exception):
    pass


def update_alert_status(db: Session, alert_id: int, status: str, version: int) -> Alert:
    """Optimistic-lock update: succeeds only if the row is still at `version`.

    Under concurrency exactly one of two same-version updates wins; the other
    sees rowcount == 0 and raises VersionConflictError (HTTP 409).
    """
    result = db.execute(
        update(Alert)
        .where(Alert.id == alert_id, Alert.version == version)
        .values(status=status, version=Alert.version + 1, updated_at=func.now())
    )
    if result.rowcount == 0:
        raise VersionConflictError(f"alert {alert_id} is no longer at version {version}")
    db.flush()
    return db.get(Alert, alert_id)
