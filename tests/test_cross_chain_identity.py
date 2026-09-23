"""资产标识防跨链碰撞测试：两条链上相同合约地址与 tokenID 是不同资产。"""
from tests.conftest import ingest, lock, mint, reconcile, set_heads


def test_same_address_on_two_chains_no_collision(client):
    """两链相同合约地址 + tokenID：资产标识含源链，分别守恒、互不混淆。"""
    shared_contract = "0xAAAA"
    events = [
        # 资产一：源链 chainA 的 (0xAAAA, 7)
        lock(msg="m-a", tx="0xlockA", source_chain="chainA",
             contract=shared_contract, token_id="7", amount=100),
        mint(msg="m-a", tx="0xmintA", source_chain="chainA",
             contract=shared_contract, token_id="7", amount=100),
        # 资产二：源链 chainB 的 (0xAAAA, 7)，反方向桥接
        lock(msg="m-b", tx="0xlockB", chain="chainB", block=150,
             source_chain="chainB", contract=shared_contract, token_id="7",
             amount=250, dest_chain="chainA"),
        mint(msg="m-b", tx="0xmintB", chain="chainA", block=250,
             source_chain="chainB", contract=shared_contract, token_id="7",
             amount=250),
    ]
    # 两条链上合约地址与 tokenID 完全相同，仅源链不同
    r = ingest(client, events)
    assert r["ingested"] == 4
    set_heads(client, a=1000, b=1000)
    result = reconcile(client)
    assert result["anomaly_count"] == 0
    assert len(result["totals"]) == 2  # 两个不同资产，未发生碰撞合并

    by_source = {t["source_chain"]: t for t in result["totals"]}
    a = by_source["chainA"]
    b = by_source["chainB"]
    assert a["locked"] == 100 and a["minted"] == 100 and a["delta"] == 0
    assert b["locked"] == 250 and b["minted"] == 250 and b["delta"] == 0


def test_cross_chain_amounts_do_not_leak(client):
    """一条链上的资产异常不会污染另一条链上同地址资产的守恒结论。"""
    shared_contract = "0xBBBB"
    events = [
        lock(msg="ok-1", tx="0xl1", source_chain="chainA", contract=shared_contract,
             token_id="9", amount=10),
        mint(msg="ok-1", tx="0xm1", source_chain="chainA", contract=shared_contract,
             token_id="9", amount=10),
        # chainB 源资产：只有铸造没有锁定（异常）
        mint(msg="bad-1", tx="0xm2", chain="chainA", block=260,
             source_chain="chainB", contract=shared_contract, token_id="9", amount=77),
    ]
    ingest(client, events)
    set_heads(client, a=1000, b=1000)
    result = reconcile(client)
    by_source = {t["source_chain"]: t for t in result["totals"]}
    assert by_source["chainA"]["delta"] == 0          # 好资产不受影响
    assert by_source["chainB"]["delta"] == -77        # 坏资产被单独标记
    types = {a["anomaly_type"] for a in client.get("/anomalies").json()}
    assert "MINT_WITHOUT_LOCK" in types
