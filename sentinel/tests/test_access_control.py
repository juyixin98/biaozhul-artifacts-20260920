from app.detection.engine import insert_alert_if_absent
from app.models import AlertRule, DepartmentMembership, UserRole
from tests.conftest import auth_headers, make_org, make_user


def _world(db):
    # Tree: /1 (HQ) -> /1/2/ (Engineering) -> /1/2/4/ (Platform)
    #                -> /1/3/ (Sales)
    make_org(db, 1, "HQ", tz="UTC", path="/1/")
    make_org(db, 2, "Engineering", tz="America/New_York", parent_id=1, path="/1/2/")
    make_org(db, 3, "Sales", tz="Asia/Shanghai", parent_id=1, path="/1/3/")
    make_org(db, 4, "Platform", tz="America/New_York", parent_id=2, path="/1/2/4/")

    admin = make_user(db, 10, "root", UserRole.admin, org_id=1)
    raul = make_user(db, 20, "raul", UserRole.manager, org_id=2)
    maya = make_user(db, 30, "maya", UserRole.manager, org_id=3)
    lee = make_user(db, 40, "lee", UserRole.analyst, org_id=1)
    db.add(DepartmentMembership(user_id=lee.id, department_id=2))  # Engineering subtree
    # direct reports of raul
    sam = make_user(db, 50, "sam", UserRole.employee, org_id=2, manager_id=20)
    evan = make_user(db, 51, "evan", UserRole.employee, org_id=4, manager_id=20)
    # indirect report (manager below raul) — must NOT be visible to raul
    lead = make_user(db, 21, "petra", UserRole.manager, org_id=4, manager_id=20)
    ned = make_user(db, 52, "ned", UserRole.employee, org_id=4, manager_id=21)
    # sales folks
    zoe = make_user(db, 60, "zoe", UserRole.employee, org_id=3, manager_id=30)
    yuki = make_user(db, 61, "yuki", UserRole.employee, org_id=3, manager_id=30)
    # an employee trying to access alert APIs
    bob = make_user(db, 70, "bob", UserRole.employee, org_id=2)
    db.flush()

    for uid in (50, 51, 52, 60, 61, 70):
        insert_alert_if_absent(
            db, rule=AlertRule.off_hours_access, dedup_key=f"a-{uid}",
            title="t", user_id=uid, severity="low", evidence={},
        )
    db.commit()


def test_admin_sees_everything(client, db):
    _world(db)
    h = auth_headers(client, "root")
    rows = client.get("/alerts?limit=100", headers=h).json()
    assert len(rows) == 6


def test_manager_sees_only_direct_reports(client, db):
    _world(db)
    h = auth_headers(client, "raul")
    rows = client.get("/alerts?limit=100", headers=h).json()
    users = {r["user_id"] for r in rows}
    assert users == {50, 51}  # ned (52) excluded — indirect report


def test_manager_detail_outside_scope_is_404(client, db):
    _world(db)
    h = auth_headers(client, "raul")
    # find zoe's alert
    zoe_alert = next(a for a in client.get("/alerts?limit=100", headers=auth_headers(client, "root")).json()
                     if a["user_id"] == 60)
    r = client.get(f"/alerts/{zoe_alert['id']}", headers=h)
    assert r.status_code == 404
    r = client.post(f"/alerts/{zoe_alert['id']}/triage", headers=h,
                    json={"status": "confirmed", "version": 1})
    assert r.status_code == 404


def test_analyst_sees_authorized_department_subtree(client, db):
    _world(db)
    h = auth_headers(client, "lee")
    rows = client.get("/alerts?limit=100", headers=h).json()
    users = {r["user_id"] for r in rows}
    # Engineering subtree: sam (50), evan (51), ned (52), bob (70); not sales
    assert users == {50, 51, 52, 70}


def test_analyst_cannot_triage_sales_alert(client, db):
    _world(db)
    h = auth_headers(client, "lee")
    all_rows = client.get("/alerts?limit=100", headers=auth_headers(client, "root")).json()
    zoe_alert = next(a for a in all_rows if a["user_id"] == 60)
    r = client.post(f"/alerts/{zoe_alert['id']}/triage", headers=h,
                    json={"status": "confirmed", "version": 1})
    assert r.status_code == 404


def test_analyst_without_memberships_sees_nothing(client, db):
    _world(db)
    make_user(db, 41, "lonely", UserRole.analyst, org_id=1)
    db.commit()
    h = auth_headers(client, "lonely")
    rows = client.get("/alerts?limit=100", headers=h).json()
    assert rows == []


def test_employee_role_forbidden_from_apis(client, db):
    _world(db)
    h = auth_headers(client, "bob")
    assert client.get("/alerts", headers=h).status_code == 403
    assert client.post("/events/batch", headers=h,
                       json={"events": []}).status_code in (400, 403)
    assert client.post("/baselines/recompute", headers=h,
                       json={"user_ids": [50]}).status_code == 403


def test_ingestion_requires_auth(client, db):
    r = client.post("/events/batch", json={"events": []})
    assert r.status_code == 401


def test_wrong_password_rejected(client, db):
    _world(db)
    r = client.post("/auth/login", json={"username": "bob", "password": "wrong"})
    assert r.status_code == 401


def test_manager_cannot_recompute_baselines(client, db):
    _world(db)
    h = auth_headers(client, "raul")
    r = client.post("/baselines/recompute", headers=h, json={"user_ids": [50]})
    assert r.status_code == 403


def test_baseline_scope_enforced(client, db):
    _world(db)
    # analyst lee may not recompute sales user zoe (60)
    h = auth_headers(client, "lee")
    r = client.post("/baselines/recompute", headers=h, json={"user_ids": [60]})
    assert r.status_code == 404
