from __future__ import annotations

from tests.conftest import ACCOUNTANT_KEY, AUDITOR_KEY, LEAD_KEY, auth, balanced_entry


def _post(client, payload, key=ACCOUNTANT_KEY, **headers):
    h = {**auth(key), **headers}
    return client.post("/entries", json=payload, headers=h)


def test_reversal_in_open_period_adjusts_actual(client, base_world):
    r = _post(client, balanced_entry("JV-ORIG", amount=800_00))
    assert r.status_code == 201, r.text

    rev = {
        "voucher_no": "JV-ORIG",
        "entry_date": "2026-09-15",
        "period_code": "2026-09",
        "reason": "Duplicate invoice recorded",
    }
    r = client.post("/entries/reversal", json=rev, headers=auth(ACCOUNTANT_KEY))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["is_reversal"] is True
    assert body["voucher_no"] == "JV-ORIG-REV"
    assert body["reverses_voucher_no"] == "JV-ORIG"
    # Original expense debit is mirrored as a credit, so actual nets to zero.
    usage = client.get(
        "/reports/budget-usage?year=2026&fund_code=GF&department_code=ADMIN&account_code=5100",
        headers=auth(AUDITOR_KEY),
    ).json()
    assert usage[0]["actual_cents"] == 0
    assert usage[0]["available_cents"] == 10_000_00


def test_duplicate_reversal_rejected(client, base_world):
    _post(client, balanced_entry("JV-DUP", amount=800_00))
    rev = {
        "voucher_no": "JV-DUP",
        "entry_date": "2026-09-15",
        "period_code": "2026-09",
        "reason": "mistake",
    }
    r1 = client.post("/entries/reversal", json=rev, headers=auth(ACCOUNTANT_KEY))
    assert r1.status_code == 201
    r2 = client.post("/entries/reversal", json=rev, headers=auth(ACCOUNTANT_KEY))
    assert r2.status_code == 409


def test_reversal_requires_reason(client, base_world):
    _post(client, balanced_entry("JV-R", amount=100))
    rev = {
        "voucher_no": "JV-R",
        "entry_date": "2026-09-15",
        "period_code": "2026-09",
        "reason": "",
    }
    r = client.post("/entries/reversal", json=rev, headers=auth(ACCOUNTANT_KEY))
    assert r.status_code == 422


def test_reversal_into_closed_period_rejected(client, base_world):
    _post(client, balanced_entry("JV-C", amount=100))
    r = client.post(
        "/periods/2026-09/close",
        json={"reason": "month end"},
        headers=auth(LEAD_KEY),
    )
    assert r.status_code == 200
    rev = {
        "voucher_no": "JV-C",
        "entry_date": "2026-09-15",
        "period_code": "2026-09",
        "reason": "need correction",
    }
    r = client.post("/entries/reversal", json=rev, headers=auth(ACCOUNTANT_KEY))
    assert r.status_code == 409


def test_reversal_unknown_voucher_404(client, base_world):
    rev = {
        "voucher_no": "JV-NOPE",
        "entry_date": "2026-09-15",
        "period_code": "2026-09",
        "reason": "x",
    }
    r = client.post("/entries/reversal", json=rev, headers=auth(ACCOUNTANT_KEY))
    assert r.status_code == 404


def test_reversal_restores_consumed_reservation(client, base_world):
    # Approve a 300.00 reservation, post against it, then reverse: reserved
    # counter should be restored and reservation marked reversed.
    r = client.post(
        "/reservations",
        json={
            "request_no": "REQ-9",
            "year": 2026,
            "fund_code": "GF",
            "department_code": "ADMIN",
            "account_code": "5100",
            "amount_cents": 300_00,
            "description": "x",
        },
        headers=auth(LEAD_KEY),
    )
    assert r.status_code == 201, r.text
    r = _post(client, balanced_entry("JV-RES", amount=300_00, reservation="REQ-9"))
    assert r.status_code == 201, r.text

    rev = {
        "voucher_no": "JV-RES",
        "entry_date": "2026-09-20",
        "period_code": "2026-09",
        "reason": "service not delivered",
    }
    r = client.post("/entries/reversal", json=rev, headers=auth(ACCOUNTANT_KEY))
    assert r.status_code == 201, r.text

    usage = client.get(
        "/reports/budget-usage?year=2026&fund_code=GF&department_code=ADMIN&account_code=5100",
        headers=auth(AUDITOR_KEY),
    ).json()[0]
    assert usage["actual_cents"] == 0
    assert usage["reserved_cents"] == 300_00
    reservation = client.get(
        "/reservations?request_no=REQ-9", headers=auth(AUDITOR_KEY)
    ).json()[0]
    assert reservation["status"] == "reversed"
    assert reservation["reversed_voucher_no"] == "JV-RES-REV"
