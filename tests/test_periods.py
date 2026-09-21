"""Period close/reopen: role checks, audit trail, and the close/post race."""
import threading

from sqlalchemy import select

from conftest import ACCOUNTANT, AUDITOR, OFFICER, make_payload

from app.errors import DomainError
from app.models import JournalEntry, Period
from app.schemas import JournalEntryCreate, JournalLineIn
from app.services.journals import post_journal
from app.services.periods import close_period


def test_close_blocks_posting(client, seed):
    r = client.post("/periods/2026/9/close", headers=OFFICER)
    assert r.status_code == 200
    assert r.json()["status"] == "closed"
    r = client.post("/journals", json=make_payload(), headers=ACCOUNTANT)
    assert r.status_code == 409


def test_double_close_conflicts(client, seed):
    client.post("/periods/2026/9/close", headers=OFFICER)
    r = client.post("/periods/2026/9/close", headers=OFFICER)
    assert r.status_code == 409


def test_close_requires_finance_officer(client, seed):
    assert client.post("/periods/2026/9/close", headers=ACCOUNTANT).status_code == 403
    assert client.post("/periods/2026/9/close", headers=AUDITOR).status_code == 403


def test_reopen_requires_finance_officer(client, seed):
    client.post("/periods/2026/9/close", headers=OFFICER)
    body = {"reason": "correction window"}
    assert client.post("/periods/2026/9/reopen", json=body, headers=ACCOUNTANT).status_code == 403
    assert client.post("/periods/2026/9/reopen", json=body, headers=AUDITOR).status_code == 403
    r = client.post("/periods/2026/9/reopen", json=body, headers=OFFICER)
    assert r.status_code == 200
    assert r.json()["status"] == "open"
    # Posting works again after reopen.
    assert client.post("/journals", json=make_payload(), headers=ACCOUNTANT).status_code == 201


def test_reopen_requires_reason(client, seed):
    client.post("/periods/2026/9/close", headers=OFFICER)
    r = client.post("/periods/2026/9/reopen", json={"reason": ""}, headers=OFFICER)
    assert r.status_code == 422


def test_close_and_reopen_leave_audit_trail(client, seed):
    client.post("/periods/2026/9/close", json={"reason": "month end"}, headers=OFFICER)
    client.post(
        "/periods/2026/9/reopen", json={"reason": "missed accrual JE-114"}, headers=OFFICER
    )
    audits = client.get("/periods/2026/9/audits", headers=AUDITOR).json()
    assert [a["action"] for a in audits] == ["close", "reopen"]
    assert audits[0]["reason"] == "month end"
    assert audits[1]["reason"] == "missed accrual JE-114"
    assert all(a["actor"] == "olivia" for a in audits)


def test_close_post_race_has_explicit_ordering(Session, db):
    """A posting and a close racing on the same period must serialize: either
    the post commits first (entry exists, close follows) or the close commits
    first (post fails 409). A posting must never land in a closed period."""
    db.add_all(
        [
            Period(year=2031, month=1), Period(year=2031, month=2),
            Period(year=2031, month=3), Period(year=2031, month=4),
            Period(year=2031, month=5), Period(year=2031, month=6),
        ]
    )
    db.commit()

    for month in range(1, 7):
        key = f"race-{month}"
        payload = JournalEntryCreate(
            year=2031,
            month=month,
            idempotency_key=key,
            lines=[
                JournalLineIn(
                    fund_code="GEN", department_code="FIN", account_code="5000",
                    debit_cents=100, credit_cents=0,
                ),
                JournalLineIn(
                    fund_code="GEN", department_code="FIN", account_code="1000",
                    debit_cents=0, credit_cents=100,
                ),
            ],
        )
        barrier = threading.Barrier(2)
        outcome = {}

        def poster():
            session = Session()
            try:
                barrier.wait(timeout=15)
                post_journal(session, payload, "amy")
                outcome["post"] = True
            except DomainError:
                outcome["post"] = False
            finally:
                session.close()

        def closer():
            session = Session()
            try:
                barrier.wait(timeout=15)
                close_period(session, 2031, month, "olivia")
                outcome["close"] = True
            except DomainError:
                outcome["close"] = False
            finally:
                session.close()

        threads = [threading.Thread(target=poster), threading.Thread(target=closer)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        check = Session()
        entry = check.scalar(
            select(JournalEntry).where(JournalEntry.idempotency_key == key)
        )
        period = check.scalar(
            select(Period).where(Period.year == 2031, Period.month == month)
        )
        # The entry exists iff the posting won the race; never otherwise.
        assert (entry is not None) == outcome["post"]
        # The close always lands eventually (fresh open period each round).
        assert period.status == "closed"
        check.close()
