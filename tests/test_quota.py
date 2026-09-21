"""Daily quota: atomic reservation under concurrency, release on failure."""

from concurrent.futures import ThreadPoolExecutor

from app.config import settings
from app.db import SessionLocal
from app.models import DailyUsage, SignRequest
from conftest import auth, make_custodial_wallet, make_draft, make_user, sign, submit

VALUE = 10**14          # 0.0001 ETH
GAS_PRICE = 10**9       # 1 gwei
GAS_LIMIT = 21000
PER_TX = VALUE + GAS_LIMIT * GAS_PRICE  # reserved per signature


def _prepare(client, key, wallet_id, count):
    drafts = []
    for i in range(count):
        d = make_draft(
            client, key, wallet_id, nonce=i,
            value_wei=str(VALUE), gas_limit=GAS_LIMIT, gas_price_wei=str(GAS_PRICE),
        )
        submit(client, key, d["id"])
        drafts.append(d["id"])
    return drafts


def _usage(wallet_id):
    db = SessionLocal()
    try:
        row = db.query(DailyUsage).filter_by(wallet_id=wallet_id).first()
        return int(row.used_wei) if row else 0
    finally:
        db.close()


def test_quota_enforced_sequentially(client, monkeypatch):
    monkeypatch.setattr(settings, "daily_limit_wei", 2 * PER_TX)
    key = make_user(client)
    wallet = make_custodial_wallet(client, key)
    drafts = _prepare(client, key, wallet["id"], 4)

    results = [sign(client, key, d, f"q-{i}") for i, d in enumerate(drafts)]
    statuses = [r.status_code for r in results]
    assert statuses.count(200) == 2
    assert statuses.count(429) == 2
    assert _usage(wallet["id"]) == 2 * PER_TX


def test_concurrent_signers_never_exceed_quota(client, monkeypatch):
    allowed = 5
    total = 16
    monkeypatch.setattr(settings, "daily_limit_wei", allowed * PER_TX)
    key = make_user(client)
    wallet = make_custodial_wallet(client, key)
    drafts = _prepare(client, key, wallet["id"], total)

    def attempt(i):
        return sign(client, key, drafts[i], f"c-{i}").status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(attempt, range(total)))

    ok = statuses.count(200)
    rejected = statuses.count(429)
    assert ok == allowed, f"expected {allowed} successes, got {ok}: {statuses}"
    assert rejected == total - allowed
    assert _usage(wallet["id"]) == allowed * PER_TX


def test_failed_signing_releases_quota(client, monkeypatch):
    import app.services as services

    monkeypatch.setattr(settings, "daily_limit_wei", PER_TX)
    key = make_user(client)
    wallet = make_custodial_wallet(client, key)
    drafts = _prepare(client, key, wallet["id"], 2)

    def boom(*a, **kw):
        raise RuntimeError("backend exploded")

    monkeypatch.setattr(services, "sign_evm_transaction", boom)
    r = sign(client, key, drafts[0], "f-1")
    assert r.status_code == 500

    # Reservation was returned: usage back to zero, request marked failed+released.
    assert _usage(wallet["id"]) == 0
    db = SessionLocal()
    try:
        req = db.query(SignRequest).filter_by(idempotency_key="f-1").one()
        assert req.status == "failed"
        assert req.quota_released is True
    finally:
        db.close()

    # Retrying the failed key replays the stored failure (no double charge).
    r2 = sign(client, key, drafts[0], "f-1")
    assert r2.status_code == 200
    assert r2.json()["status"] == "failed"

    # A fresh key on the same nonce is allowed after failure and now succeeds.
    monkeypatch.undo()
    monkeypatch.setattr(settings, "daily_limit_wei", PER_TX)
    r3 = sign(client, key, drafts[0], "f-2")
    assert r3.status_code == 200, r3.text
    assert r3.json()["status"] == "succeeded"
    assert _usage(wallet["id"]) == PER_TX


def test_cooldown_between_signatures(client, monkeypatch):
    monkeypatch.setattr(settings, "cooldown_seconds", 3600.0)
    key = make_user(client)
    wallet = make_custodial_wallet(client, key)
    drafts = _prepare(client, key, wallet["id"], 2)

    assert sign(client, key, drafts[0], "cd-1").status_code == 200
    r = sign(client, key, drafts[1], "cd-2")
    assert r.status_code == 429
    assert "cooldown" in r.json()["detail"]
