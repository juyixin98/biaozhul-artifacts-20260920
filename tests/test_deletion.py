"""主体删除：清除可识别映射与导出副本；审计只留不含个人字段的记录。"""

import json

from .conftest import grant_payload

SUBJECT = "erase-me@x.test"


def _setup(client, admin, seeded):
    client.post("/consents/events", headers=admin,
                json=grant_payload("e1", SUBJECT, seeded["pv1"].id, 0))
    r = client.post(f"/subjects/{SUBJECT}/exports", headers=admin)
    assert r.status_code == 201
    return r.json()["export_id"]


def test_export_and_delete(client, admin, auditor, seeded):
    _setup(client, admin, seeded)
    exports = client.get(f"/subjects/{SUBJECT}/exports", headers=auditor).json()["exports"]
    assert len(exports) == 1
    assert exports[0]["payload"]["subject_ref"] == SUBJECT

    r = client.delete(f"/subjects/{SUBJECT}", headers=admin)
    assert r.status_code == 200
    assert r.json()["exports_removed"] == 1

    # 删除后：映射与导出副本均不可查
    assert client.get(f"/subjects/{SUBJECT}/exports", headers=auditor).status_code == 404
    assert client.get("/consents/verify", headers=auditor,
                      params={"subject_ref": SUBJECT,
                              "purpose_code": "marketing"}).status_code == 404
    assert client.get("/consents/history", headers=auditor,
                      params={"subject_ref": SUBJECT,
                              "purpose_code": "marketing"}).status_code == 404
    # 重复删除 -> 404
    assert client.delete(f"/subjects/{SUBJECT}", headers=admin).status_code == 404


def test_audit_log_has_no_personal_fields(client, admin, auditor, seeded):
    _setup(client, admin, seeded)
    client.delete(f"/subjects/{SUBJECT}", headers=admin)

    entries = client.get("/audit-log", headers=auditor).json()["entries"]
    actions = {e["action"] for e in entries}
    assert "consent.grant" in actions
    assert "subject.exported" in actions
    assert "subject.deleted" in actions
    # 审计记录中不得出现可识别的 subject_ref
    assert SUBJECT not in json.dumps(entries)


def test_events_survive_subject_deletion_as_pseudonymous_history(
        client, admin, seeded, db_session):
    from sqlalchemy import select

    from app.models import ConsentEvent

    _setup(client, admin, seeded)
    client.delete(f"/subjects/{SUBJECT}", headers=admin)
    # 不可变历史保留（只剩不可再关联的 subject_id）
    events = db_session.scalars(select(ConsentEvent)).all()
    assert len(events) == 1
    assert events[0].event_id == "e1"
