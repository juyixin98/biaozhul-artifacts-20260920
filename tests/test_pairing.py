"""配对与乱序测试。"""
from tests.conftest import (anomalies, burn, ingest, lock, mint, reconcile,
                            release, set_heads)


def test_lock_mint_paired_and_conserved(client):
    """完整 锁定->铸造->销毁->释放 生命周期守恒，无异常。"""
    ingest(client, [lock(), mint(), burn(), release()])
    set_heads(client, a=1000, b=1000)
    r = reconcile(client)
    assert r["pairs_formed"] == 2
    assert r["anomaly_count"] == 0
    totals = r["totals"]
    assert len(totals) == 1
    t = totals[0]
    assert (t["locked"], t["released"], t["minted"], t["burned"]) == (100, 100, 100, 100)
    assert t["delta"] == 0


def test_out_of_order_delivery(client):
    """铸造先于锁定到达：先报无锁定铸造（如实记录），锁定到达并确认后恢复守恒。"""
    ingest(client, [mint(block=200)])
    set_heads(client, a=0, b=202)
    r1 = reconcile(client)
    types1 = {a["anomaly_type"] for a in anomalies(client, r1["snapshot_id"])}
    assert "MINT_WITHOUT_LOCK" in types1  # 当时确实没有任何锁定，如实报告

    # 锁定事件乱序迟到
    ingest(client, [lock(block=100)])
    set_heads(client, a=102, b=202)
    r2 = reconcile(client)
    assert r2["pairs_formed"] == 1
    assert r2["anomaly_count"] == 0
    assert r2["totals"][0]["delta"] == 0
    # 历史快照保留，第一次的异常记录仍可查
    snap1 = client.get(f"/snapshots/{r1['snapshot_id']}").json()
    assert snap1["anomaly_count"] > 0


def test_burn_release_paired(client):
    """赎回方向：销毁与释放按消息配对。"""
    ingest(client, [lock(), mint(), burn(), release()])
    set_heads(client, a=1000, b=1000)
    r = reconcile(client)
    assert r["pairs_formed"] == 2
    assets = client.get("/assets").json()
    assert assets[0]["net_locked"] == 0 and assets[0]["net_minted"] == 0
