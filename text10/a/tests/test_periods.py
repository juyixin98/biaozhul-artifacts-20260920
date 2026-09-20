import threading

from app.errors import DomainError
from app.models import FiscalPeriod, JournalEntry, PeriodAuditLog
from app.schemas import JournalCreate, JournalLineIn
from app.services.journals import post_journal
from app.services.periods import close_period

from .conftest import CLERK, MANAGER, TestingSession, journal_payload


def test_close_blocks_posting(client, refs):
    assert client.post(f"/periods/{refs.period.id}/close", headers=MANAGER).status_code == 200
    r = client.post("/journals", json=journal_payload(refs), headers=CLERK)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "period-closed"


def test_double_close_rejected(client, refs):
    assert client.post(f"/periods/{refs.period.id}/close", headers=MANAGER).status_code == 200
    r = client.post(f"/periods/{refs.period.id}/close", headers=MANAGER)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "period-already-closed"


def test_close_requires_finance_manager(client, refs):
    r = client.post(f"/periods/{refs.period.id}/close", headers=CLERK)
    assert r.status_code == 403


def test_reopen_requires_finance_manager(client, refs):
    client.post(f"/periods/{refs.period.id}/close", headers=MANAGER)
    r = client.post(
        f"/periods/{refs.period.id}/reopen", json={"reason": "fixing misclassification"}, headers=CLERK
    )
    assert r.status_code == 403


def test_reopen_requires_reason(client, refs):
    client.post(f"/periods/{refs.period.id}/close", headers=MANAGER)
    r = client.post(f"/periods/{refs.period.id}/reopen", json={"reason": ""}, headers=MANAGER)
    assert r.status_code == 422


def test_reopen_writes_audit_log(client, refs):
    client.post(f"/periods/{refs.period.id}/close", headers=MANAGER)
    r = client.post(
        f"/periods/{refs.period.id}/reopen",
        json={"reason": "correction of misposted voucher"},
        headers=MANAGER,
    )
    assert r.status_code == 200
    assert r.json()["status"] == "open"
    audits = client.get(f"/periods/{refs.period.id}/audits", headers=MANAGER).json()
    assert [a["action"] for a in audits] == ["close", "reopen"]
    assert audits[1]["reason"] == "correction of misposted voucher"
    assert audits[1]["actor"] == "cfo"


def test_reopen_allows_posting_again(client, refs):
    client.post(f"/periods/{refs.period.id}/close", headers=MANAGER)
    client.post(
        f"/periods/{refs.period.id}/reopen", json={"reason": "reopen for audit adjustment"}, headers=MANAGER
    )
    r = client.post("/journals", json=journal_payload(refs), headers=CLERK)
    assert r.status_code == 201


def test_close_and_post_race_has_explicit_order(refs):
    """Close and post take the same row lock: one of two explicit orders
    happens — post-then-close (entry exists, period closed) or
    close-then-post (post rejected, period closed). Never anything else."""
    results = {}
    barrier = threading.Barrier(2)

    def do_post():
        session = TestingSession()
        try:
            barrier.wait(timeout=10)
            payload = JournalCreate(
                idempotency_key="race-post",
                period_id=refs.period.id,
                memo="race",
                lines=[
                    JournalLineIn(
                        fund_id=refs.fund.id, department_id=refs.dept.id,
                        account_id=refs.expense.id, debit_cents=100,
                    ),
                    JournalLineIn(
                        fund_id=refs.fund.id, department_id=refs.dept.id,
                        account_id=refs.cash.id, credit_cents=100,
                    ),
                ],
            )
            post_journal(session, payload, "racer")
            session.commit()
            results["post"] = "ok"
        except DomainError as e:
            session.rollback()
            results["post"] = e.code
        finally:
            session.close()

    def do_close():
        session = TestingSession()
        try:
            barrier.wait(timeout=10)
            close_period(session, refs.period.id, "cfo")
            session.commit()
            results["close"] = "ok"
        except DomainError as e:
            session.rollback()
            results["close"] = e.code
        finally:
            session.close()

    threads = [threading.Thread(target=do_post), threading.Thread(target=do_close)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    check = TestingSession()
    period = check.get(FiscalPeriod, refs.period.id)
    entries = check.query(JournalEntry).filter_by(idempotency_key="race-post").count()
    check.close()

    assert results["close"] == "ok"
    assert period.status == "closed"
    if results["post"] == "ok":
        # post won the lock first: entry exists, then close took effect
        assert entries == 1
    else:
        # close won first: post was rejected, nothing written
        assert results["post"] == "period-closed"
        assert entries == 0
