"""验收场景：空记录、第一块、块间空隙、大量同块更新、未来块拒绝。"""
from __future__ import annotations

from fastapi.testclient import TestClient


def test_health(client: TestClient, chain_client) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["chainId"] == 31337
    assert body["contract"].lower() == chain_client.contract.address.lower()


def test_empty_record(client: TestClient) -> None:
    # 空记录：查询返回 0 / found=false
    r = client.get("/history")
    assert r.status_code == 200
    assert r.json() == []

    r = client.get("/latest")
    assert r.status_code == 200
    assert r.json() == {"blockNumber": 0, "value": 0, "exists": False}

    r = client.get("/checkpoints/0")
    assert r.status_code == 200
    body = r.json()
    assert body["value"] == 0
    assert body["found"] is False


def test_first_checkpoint(client: TestClient, chain) -> None:
    # Anvil 中交易在「最新块 + 1」打包：推进到 99，下一笔即落在 100
    chain.mine_to(99)
    assert chain.block == 99
    r = client.post("/checkpoints", json={"value": 42})
    assert r.status_code == 201
    body = r.json()
    assert body["value"] == 42
    assert body["blockNumber"] == 100
    assert body["status"] == 1
    assert chain.block == 100

    # 早于第一个检查点：无值
    early = client.get("/checkpoints/99").json()
    assert early["value"] == 0
    assert early["found"] is False

    # 恰好第一块
    assert client.get("/checkpoints/100").json()["value"] == 42

    # 之后的块（无新检查点）持续返回 42
    chain.mine(50)
    assert client.get("/checkpoints/101").json()["value"] == 42
    assert client.get("/checkpoints/150").json()["value"] == 42

    r = client.get("/latest")
    assert r.json() == {"blockNumber": 100, "value": 42, "exists": True}


def test_gaps_between_blocks(client: TestClient, chain) -> None:
    base = chain.block

    def set_when_next_block_is(target_block: int, value: int) -> None:
        # 让下一笔交易打包进 target_block
        chain.mine_to(target_block - 1)
        r = client.post("/checkpoints", json={"value": value})
        assert r.status_code == 201
        assert r.json()["blockNumber"] == target_block

    b1 = base + 5
    b2 = b1 + 10
    b3 = b2 + 30
    set_when_next_block_is(b1, 1)
    set_when_next_block_is(b2, 2)
    set_when_next_block_is(b3, 3)

    # 再推进 100 个块，之后才能查询「不晚于未来点」
    chain.mine(100)

    def value_at(b: int) -> int:
        return client.get(f"/checkpoints/{b}").json()["value"]

    # 空隙中返回空隙前最后值
    assert value_at(b1 - 1) == 0
    assert value_at(b1) == 1
    assert value_at(b1 + 1) == 1
    assert value_at(b2 - 1) == 1
    assert value_at(b2) == 2
    assert value_at(b2 + 1) == 2
    assert value_at(b3 - 1) == 2
    assert value_at(b3) == 3
    assert value_at(b3 + 100) == 3

    history = client.get("/history").json()
    assert [h["value"] for h in history] == [1, 2, 3]
    assert [h["blockNumber"] for h in history] == [b1, b2, b3]


def test_many_updates_same_block_merge(client: TestClient, chain, chain_client) -> None:
    # automine 关闭时 HTTP 请求会阻塞等待回执，因此直接向交易池发送
    # 原始交易（gasPrice 逐笔递增以避免 underpriced 拒绝），再统一打包。
    from tests.conftest import fire_set_value

    target_block = chain.block + 1
    chain.set_automine(False)
    try:
        n = 100
        for i in range(1, n + 1):
            fire_set_value(chain_client, i)
        # 所有交易在同一个区块中打包（不重开 automine，避免额外出块）
        mined_block = chain.batch_mine_pending()
    finally:
        chain.set_automine(True)

    assert mined_block == target_block
    assert chain.block == target_block

    history = client.get("/history").json()
    assert len(history) == 1, "同块更新必须合并为一个检查点"
    assert history[0] == {"blockNumber": target_block, "value": n}

    body = client.get(f"/checkpoints/{target_block}").json()
    assert body["value"] == n  # 最后一次写入生效


def test_future_block_rejected(client: TestClient, chain) -> None:
    current = chain.block
    r = client.get(f"/checkpoints/{current + 5}")
    assert r.status_code == 400
    body = r.json()
    assert body["error"] == "future_block"
    assert body["requested"] == current + 5
    assert body["current"] == current

    # 当前块本身合法
    assert client.get(f"/checkpoints/{current}").status_code == 200


def test_value_validation(client: TestClient) -> None:
    r = client.post("/checkpoints", json={"value": -1})
    assert r.status_code == 422
    r = client.post("/checkpoints", json={"value": 2**224})
    assert r.status_code == 422
    r = client.post("/checkpoints", json={"value": 2**224 - 1})
    assert r.status_code == 201


def test_negative_block_path(client: TestClient) -> None:
    r = client.get("/checkpoints/-1")
    # FastAPI 路径类型为 int，负数不匹配 → 404；服务不应 500
    assert r.status_code in (404, 422)
