"""摄取去重与确认高度测试。"""
from tests.conftest import ingest, lock, mint, reconcile, set_heads


def test_duplicate_delivery_not_counted_twice(client):
    """同一事件重复投递（同批与跨批）只入账一次。"""
    ev = lock()
    r1 = ingest(client, [ev, dict(ev)])          # 同批重复
    assert r1["ingested"] == 1 and r1["duplicates"] == 1
    r2 = ingest(client, [ev])                    # 跨批重复投递
    assert r2["ingested"] == 0 and r2["duplicates"] == 1

    set_heads(client, a=1000, b=1000)
    reconcile(client)
    ledger = client.get("/ledger").json()
    assert len(ledger) == 1
    assert ledger[0]["entry_type"] == "LOCK" and ledger[0]["amount"] == 100


def test_event_enters_ledger_only_after_confirmations(client):
    """未达确认高度不入正式账，达到后才入账。"""
    ingest(client, [lock(block=100)])
    set_heads(client, a=101, b=0)  # 需要 100+2=102
    r = reconcile(client)
    assert r["newly_confirmed"] == 0
    assert client.get("/ledger").json() == []

    set_heads(client, a=102)
    r = reconcile(client)
    assert r["newly_confirmed"] == 1
    ledger = client.get("/ledger").json()
    assert len(ledger) == 1 and ledger[0]["status"] == "ACTIVE"


def test_reconcile_is_idempotent_for_ledger(client):
    """重复对账不会重复入账，但每次都保留快照。"""
    ingest(client, [lock(), mint()])
    set_heads(client, a=1000, b=1000)
    reconcile(client)
    reconcile(client)
    assert len(client.get("/ledger").json()) == 2
    assert len(client.get("/snapshots").json()) == 2  # 每次对账快照均保留
