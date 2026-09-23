"""终局冲突：第二个 2/3 联盟出现 → 告警、冻结、证据完整、检查点不被覆盖。"""
from __future__ import annotations

from .conftest import BLOCK_A, BLOCK_B, create_epoch, make_validators, post_vote, vote_payload


def _setup_conflict(client):
    # 4 名验证者各权重 1，总 4，需 >=3
    validators, keys = make_validators([("a", 1), ("b", 1), ("c", 1), ("d", 1)])
    create_epoch(client, 60, validators)
    # a,b,c 投 A → A 终局（有效权重 3 > 8/3）
    for v in ("a", "b", "c"):
        post_vote(client, vote_payload(keys, 60, v, BLOCK_A))
    assert client.get("/epochs/60/status").json()["state"] == "finalized"
    # a,b 双投 B，d 投 B → B 的表观权重也是 3：第二个 2/3 联盟
    post_vote(client, vote_payload(keys, 60, "a", BLOCK_B))
    post_vote(client, vote_payload(keys, 60, "b", BLOCK_B))
    r = post_vote(client, vote_payload(keys, 60, "d", BLOCK_B))
    return keys, r


def test_finality_conflict_freezes_and_keeps_evidence(client):
    keys, r = _setup_conflict(client)
    assert r.json()["epoch_state"] == "frozen_conflict"

    st = client.get("/epochs/60/status").json()
    assert st["state"] == "frozen_conflict"
    # 检查点不被最新消息覆盖：仍是 A
    assert st["finalized_block"] == BLOCK_A
    assert st["finalized_weight"] == 3
    # 冲突证据
    cf = st["conflict"]
    assert cf is not None
    assert cf["finalized_block"] == BLOCK_A
    assert cf["conflicting_block"] == BLOCK_B
    assert cf["conflicting_apparent_weight"] == 3
    assert sorted(cf["equivocators"]) == ["a", "b"]
    assert len(cf["votes_for"][BLOCK_A]) == 3
    assert len(cf["votes_for"][BLOCK_B]) == 3

    # 证据端点可独立验证：双投者的两条签名票都在
    ev = client.get("/epochs/60/evidence").json()
    assert ev["conflict"] is not None
    equiv_voters = {e["validator_id"] for e in ev["equivocations"]}
    assert equiv_voters == {"a", "b"}
    for e in ev["equivocations"]:
        assert len(e["votes"]) == 2
        assert {v["block_hash"] for v in e["votes"]} == {BLOCK_A, BLOCK_B}

    # 冻结：后续投票被拒绝
    r2 = post_vote(client, vote_payload(keys, 60, "c", BLOCK_B))
    assert r2.status_code == 409
    assert "冻结" in r2.json()["reason"]

    # 检查点列表仍然只有 A
    cps = client.get("/checkpoints").json()
    assert len(cps) == 1
    assert cps[0]["epoch"] == 60
    assert cps[0]["block_hash"] == BLOCK_A


def test_no_conflict_when_second_block_below_threshold(client):
    # a,b,c 投 A 终局；a 双投 B、b 投 B：B 表观权重 2 < 3，无冲突
    validators, keys = make_validators([("a", 1), ("b", 1), ("c", 1), ("d", 1)])
    create_epoch(client, 61, validators)
    for v in ("a", "b", "c"):
        post_vote(client, vote_payload(keys, 61, v, BLOCK_A))
    post_vote(client, vote_payload(keys, 61, "a", BLOCK_B))
    post_vote(client, vote_payload(keys, 61, "b", BLOCK_B))
    st = client.get("/epochs/61/status").json()
    assert st["state"] == "finalized"
    assert st["conflict"] is None
    assert sorted(e["validator_id"] for e in st["equivocations"]) == ["a", "b"]
