"""HTTP 端到端：登记链/feeder/块、投递已签名事件、触发对账、取快照与证据。"""
from __future__ import annotations

from datetime import timedelta

from app.demo_crypto import DemoFeeder
from tests.factories import BASE_TIME, block_hash, make_event, ZERO


def test_api_full_flow(client):
    fa, fb = DemoFeeder("fa"), DemoFeeder("fb")

    assert client.get("/health").json()["status"] == "ok"

    # 登记链
    r = client.post("/chains", json={
        "chain_id": "chainA", "name": "A", "confirmation_depth": 2,
        "message_timeout_seconds": 3600,
    })
    assert r.status_code == 201, r.text
    r = client.post("/chains", json={
        "chain_id": "chainB", "name": "B", "confirmation_depth": 2,
        "message_timeout_seconds": 3600,
    })
    assert r.status_code == 201

    # 注册 feeder 公钥
    assert client.post("/chains/chainA/feeders",
                       json={"public_key_hex": fa.public_key, "label": "fa"}
                       ).status_code == 201
    assert client.post("/chains/chainB/feeders",
                       json={"public_key_hex": fb.public_key, "label": "fb"}
                       ).status_code == 201

    # 登记块头并设置 tip
    def blocks(cid):
        return {
            "tip_block_hash": block_hash(cid, 4),
            "blocks": [
                {"block_height": h,
                 "block_hash": block_hash(cid, h),
                 "parent_hash": ZERO if h == 0 else block_hash(cid, h - 1),
                 "block_time": (BASE_TIME + timedelta(seconds=12 * h)).isoformat()}
                for h in range(0, 5)
            ],
        }
    assert client.post("/chains/chainA/blocks", json=blocks("chainA")).status_code == 200
    assert client.post("/chains/chainB/blocks", json=blocks("chainB")).status_code == 200

    # 无锁定铸造
    mint = make_event(fb, chain_id="chainB", event_type="MINT", nonce="m-1",
                      source_chain="chainA", contract="0xC0FFEE", amount=500,
                      block_height=1)
    r = client.post("/events", json={"events": [mint]})
    assert r.status_code == 200 and r.json()[0]["status"] == "accepted", r.text

    # 重复投递幂等
    r = client.post("/events", json={"events": [mint, mint]})
    assert {x["status"] for x in r.json()} == {"duplicate"}

    # 伪造签名被拒
    bad = dict(mint)
    bad["payload"] = {**mint["payload"], "amount": "501"}
    r = client.post("/events", json={"events": [bad]})
    assert r.json()[0]["status"] == "rejected"
    assert "signature" in r.json()[0]["error"]

    # 第一次对账：此刻只有 MINT，含 MINT_WITHOUT_LOCK 历史结论
    r = client.post("/reconciliations", json={})
    assert r.status_code == 201, r.text
    first = r.json()
    assert first["finding_count"] >= 1
    assert any(f["code"] == "MINT_WITHOUT_LOCK" for f in first["findings"])

    # 补上 LOCK（乱序），第二次对账：干净配对
    lock = make_event(fa, chain_id="chainA", event_type="LOCK", nonce="m-1",
                      source_chain="chainA", contract="0xC0FFEE", amount=500,
                      block_height=1)
    assert client.post("/events", json={"events": [lock]}).json()[0]["status"] == "accepted"

    r = client.post("/reconciliations", json={})
    second = r.json()
    assert second["finding_count"] == 0, second
    assert second["prev_snapshot_hash"] == first["snapshot_hash"]

    # 列表/详情/发现 查询
    lst = client.get("/reconciliations?limit=10").json()
    assert len(lst) == 2
    detail = client.get(f"/reconciliations/{second['run_id']}").json()
    assert detail["snapshot_hash"] == second["snapshot_hash"]

    findings = client.get(f"/findings?run_id={first['run_id']}&code=MINT_WITHOUT_LOCK").json()
    assert len(findings) == 1
    assert client.get("/stats").json()["reconciliations"] == 2


def test_api_unknown_chain_and_block_tip_validation(client):
    r = client.post("/chains/chainX/feeders",
                    json={"public_key_hex": "ab" * 32, "label": "x"})
    assert r.status_code == 404

    client.post("/chains", json={"chain_id": "chainA", "confirmation_depth": 1})
    r = client.post("/chains/chainA/blocks", json={
        "tip_block_hash": "0xmissing",
        "blocks": [{"block_height": 0, "block_hash": "0xb0", "parent_hash": "0xp",
                    "block_time": BASE_TIME.isoformat()}],
    })
    # tip 指向未登记块会报错（必须先随批次登记）
    assert r.status_code == 400


def test_api_admin_token_enforced(client, monkeypatch):
    from app.config import get_settings
    # 未设置 token 时不受限
    assert client.post("/chains", json={"chain_id": "free"}).status_code == 201
