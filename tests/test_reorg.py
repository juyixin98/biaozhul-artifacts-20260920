"""分叉撤销测试。"""
from tests.conftest import anomalies, ingest, lock, mint, reconcile, set_heads


def test_reorg_reverts_mint_and_ledger(client):
    """铸造确认后所在链分叉：事件与账目均标记 REVERTED，守恒视图随之变化。"""
    ingest(client, [lock(block=100), mint(block=200)])
    set_heads(client, a=1000, b=1000)
    r1 = reconcile(client)
    assert r1["anomaly_count"] == 0
    assert r1["totals"][0]["delta"] == 0

    # chainB 从高度 200 起分叉，铸造被回滚
    r = client.post("/chains/chainB/reorg", json={"from_height": 200})
    assert r.status_code == 200
    body = r.json()
    assert body["events_reverted"] == 1
    assert body["ledger_entries_reverted"] == 1
    assert body["new_head"] == 199

    ledger = client.get("/ledger").json()
    mint_entries = [e for e in ledger if e["entry_type"] == "MINT"]
    assert len(mint_entries) == 1 and mint_entries[0]["status"] == "REVERTED"

    # 再次对账：锁定失去配对，超时后报 UNMATCHED_LOCK
    set_heads(client, b=1000)
    r2 = reconcile(client, timeout=50)
    types = {a["anomaly_type"] for a in anomalies(client, r2["snapshot_id"])}
    assert "UNMATCHED_LOCK" in types
    assert r2["totals"][0]["net_minted"] == 0
    assert r2["totals"][0]["delta"] == 100


def test_reorg_then_replacement_event(client):
    """分叉后新链上的替代事件（新交易哈希）可重新入账并配对。"""
    ingest(client, [lock(block=100), mint(block=200, tx="0xmint_old")])
    set_heads(client, a=1000, b=1000)
    reconcile(client)

    client.post("/chains/chainB/reorg", json={"from_height": 200})
    # 新链在高度 205 重新打包了同一消息的铸造（新 tx_hash）
    ingest(client, [mint(block=205, tx="0xmint_new")])
    set_heads(client, b=1000)
    r = reconcile(client)
    assert r["anomaly_count"] == 0
    assert r["totals"][0]["delta"] == 0
    ledger = client.get("/ledger").json()
    statuses = sorted(e["status"] for e in ledger if e["entry_type"] == "MINT")
    assert statuses == ["ACTIVE", "REVERTED"]  # 旧账目保留审计轨迹
