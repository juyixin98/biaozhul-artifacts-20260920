"""超时未配对、重复铸造、无锁定铸造的异常证据测试。"""
from tests.conftest import (anomalies, burn, ingest, lock, mint, reconcile,
                            set_heads)


def test_unmatched_lock_after_timeout(client):
    """锁定后对端链高度超过超时阈值仍无铸造 -> UNMATCHED_LOCK，证据指向原始锁定事件。"""
    ingest(client, [lock(block=100)])
    set_heads(client, a=102, b=1000)  # chainB 高度 1000 >= 100 + timeout(50)
    r = reconcile(client, timeout=50)
    ans = anomalies(client, r["snapshot_id"])
    unmatched = [a for a in ans if a["anomaly_type"] == "UNMATCHED_LOCK"]
    assert len(unmatched) == 1
    ev = unmatched[0]["evidence"][0]
    assert ev["chain"] == "chainA" and ev["tx_hash"] == "0xlock1"
    assert ev["block_number"] == 100 and ev["event_type"] == "LOCK"
    # 同时触发守恒异常
    assert any(a["anomaly_type"] == "CONSERVATION_MISMATCH" for a in ans)


def test_lock_within_timeout_not_flagged(client):
    """对端链高度未超阈值时不报超时。"""
    ingest(client, [lock(block=100)])
    set_heads(client, a=102, b=120)  # 120 < 100 + 50
    r = reconcile(client, timeout=50)
    ans = anomalies(client, r["snapshot_id"])
    assert not any(a["anomaly_type"] == "UNMATCHED_LOCK" for a in ans)


def test_unmatched_burn_after_timeout(client):
    """销毁后源链超时未释放 -> UNMATCHED_BURN。"""
    ingest(client, [lock(), mint(), burn(block=300)])
    set_heads(client, a=1000, b=1000)
    r = reconcile(client, timeout=50)
    ans = anomalies(client, r["snapshot_id"])
    unmatched = [a for a in ans if a["anomaly_type"] == "UNMATCHED_BURN"]
    assert len(unmatched) == 1
    assert unmatched[0]["evidence"][0]["event_type"] == "BURN"


def test_duplicate_mint(client):
    """同一关联消息两次铸造 -> DUPLICATE_MINT，证据包含锁定与两笔铸造。"""
    ingest(client, [lock(), mint(tx="0xmint1", block=200),
                    mint(tx="0xmint2", block=201)])
    set_heads(client, a=1000, b=1000)
    r = reconcile(client)
    ans = anomalies(client, r["snapshot_id"])
    dup = [a for a in ans if a["anomaly_type"] == "DUPLICATE_MINT"]
    assert len(dup) == 1
    ev_types = sorted(e["event_type"] for e in dup[0]["evidence"])
    assert ev_types == ["LOCK", "MINT", "MINT"]
    # 净铸造 200 > 净锁定 100，不守恒
    assert r["totals"][0]["delta"] == -100


def test_mint_without_lock(client):
    """铸造无任何锁定 -> MINT_WITHOUT_LOCK，不自动修正，仅输出证据。"""
    ingest(client, [mint(tx="0xmintX", block=200)])
    set_heads(client, a=1000, b=1000)
    r = reconcile(client)
    ans = anomalies(client, r["snapshot_id"])
    mwl = [a for a in ans if a["anomaly_type"] == "MINT_WITHOUT_LOCK"]
    assert len(mwl) == 1
    assert mwl[0]["evidence"][0]["tx_hash"] == "0xmintX"
    assert r["totals"][0]["delta"] == -100
    # 服务不触碰链上状态：只产出快照与异常记录
    assert client.get("/ledger").json()[0]["entry_type"] == "MINT"
