import concurrent.futures
from app.detection.engine import insert_alert_if_absent
from app.models import Alert, AlertRule, AlertStatus, UserRole
from tests.conftest import auth_headers, make_org, make_user


def _alert(db, user_id=5):
    return insert_alert_if_absent(
        db, rule=AlertRule.off_hours_access, dedup_key=f"k:{user_id}",
        title="t", user_id=user_id, severity="low", evidence={},
    )


def test_concurrent_triage_one_winner_one_409(db):
    """Two sessions triage the same open alert with version 1: exactly one wins."""
    from app.database import SessionLocal
    from app.core.triage import triage_alert

    make_org(db, 1)
    make_user(db, 1, "root", UserRole.admin, org_id=1)
    make_user(db, 5, "sam", UserRole.employee, org_id=1)
    alert_id = _alert(db)
    db.commit()

    def act(target_status):
        s = SessionLocal()
        try:
            a = s.get(Alert, alert_id)
            triage_alert(s, alert=a, actor_id=1, new_status=target_status,
                         expected_version=1, note="n")
            s.commit()
            return 200, target_status
        except Exception as exc:
            s.rollback()
            status_code = getattr(exc, "status_code", 500)
            return status_code, None
        finally:
            s.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(act, AlertStatus.confirmed)
        f2 = pool.submit(act, AlertStatus.false_positive)
        results = [f1.result(), f2.result()]

    codes = sorted(r[0] for r in results)
    assert codes == [200, 409]

    db.expire_all()
    alert = db.get(Alert, alert_id)
    assert alert.version == 2
    assert alert.status in (AlertStatus.confirmed, AlertStatus.false_positive)
    # only the winner wrote an investigation
    from sqlalchemy import func, select
    from app.models import AlertInvestigation
    assert db.scalar(select(func.count()).select_from(AlertInvestigation)) == 1


def test_stale_version_over_http(client, db):
    make_org(db, 1)
    make_user(db, 1, "root", UserRole.admin, org_id=1)
    make_user(db, 5, "sam", UserRole.employee, org_id=1)
    aid = _alert(db)
    db.commit()
    h = auth_headers(client, "root")

    r1 = client.post(f"/alerts/{aid}/triage", headers=h,
                     json={"status": "confirmed", "version": 1})
    assert r1.status_code == 200 and r1.json()["version"] == 2

    r2 = client.post(f"/alerts/{aid}/triage", headers=h,
                     json={"status": "false_positive", "version": 1})
    assert r2.status_code == 409
    assert r2.json()["detail"]["error"] == "version_conflict"

    # advancing with current version succeeds and appends another audit row
    r3 = client.post(f"/alerts/{aid}/triage", headers=h,
                     json={"status": "investigated", "version": 2, "note": "done"})
    assert r3.status_code == 200 and r3.json()["status"] == "investigated"


def test_cannot_reopen_alert(client, db):
    make_org(db, 1)
    make_user(db, 1, "root", UserRole.admin, org_id=1)
    make_user(db, 5, "sam", UserRole.employee, org_id=1)
    aid = _alert(db)
    db.commit()
    h = auth_headers(client, "root")
    client.post(f"/alerts/{aid}/triage", headers=h, json={"status": "confirmed", "version": 1})
    r = client.post(f"/alerts/{aid}/triage", headers=h,
                    json={"status": "open", "version": 2})
    assert r.status_code == 400
