"""重复投票与双投：重复票只计一次；双投留证据并按规则排除权重。"""
from __future__ import annotations

from .conftest import BLOCK_A, BLOCK_B, create_epoch, make_validators, post_vote, vote_payload


def test_duplicate_vote_counted_once(client):
    validators, keys = make_validators([("a", 1), ("b", 1), ("c", 1)])
    create_epoch(client, 50, validators)

    r1 = post_vote(client, vote_payload(keys, 50, "a", BLOCK_A))
    assert r1.status_code == 200
    r2 = post_vote(client, vote_payload(keys, 50, "a", BLOCK_A))
    assert r2.status_code == 422
    assert r2.json()["accepted"] is False
    assert "重复" in r2.json()["reason"]

    st = client.get("/epochs/50/status").json()
    assert st["accepted_votes"] == 1
    assert st["effective_tally"][0]["weight"] == 1  # 只计一次


def test_equivocation_excludes_weight_and_keeps_evidence(client):
    # 4 名验证者各权重 1，总 4，需 >8/3 即 >=3
    validators, keys = make_validators([("a", 1), ("b", 1), ("c", 1), ("d", 1)])
    create_epoch(client, 51, validators)

    # a 双投 A 和 B
    post_vote(client, vote_payload(keys, 51, "a", BLOCK_A))
    post_vote(client, vote_payload(keys, 51, "a", BLOCK_B))
    # b、c 投 A
    post_vote(client, vote_payload(keys, 51, "b", BLOCK_A))
    post_vote(client, vote_payload(keys, 51, "c", BLOCK_A))

    st = client.get("/epochs/51/status").json()
    # 有效账本：a 被排除，A 只有 b+c = 2 票 < 3，不终局
    assert st["state"] == "pending"
    eff = {t["block_hash"]: t["weight"] for t in st["effective_tally"]}
    assert eff[BLOCK_A] == 2
    assert eff.get(BLOCK_B, 0) == 0  # 双投者的票不进入有效账本
    # 表观账本：保留全部投票（含双投者），用于观察冲突
    app = {t["block_hash"]: t["weight"] for t in st["apparent_tally"]}
    assert app[BLOCK_A] == 3
    assert app[BLOCK_B] == 1

    # 证据完整落库
    assert len(st["equivocations"]) == 1
    eq = st["equivocations"][0]
    assert eq["validator_id"] == "a"
    assert set(eq["block_hashes"]) == {BLOCK_A, BLOCK_B}
    assert len(eq["vote_ids"]) == 2

    ev = client.get("/epochs/51/evidence").json()
    assert len(ev["equivocations"]) == 1
    votes = ev["equivocations"][0]["votes"]
    assert len(votes) == 2
    assert {v["block_hash"] for v in votes} == {BLOCK_A, BLOCK_B}
    assert all(v["signature"] for v in votes)  # 原始签名保留，可独立验证

    # d 补票后 A 达到 3 票终局（a 的权重仍被排除）
    post_vote(client, vote_payload(keys, 51, "d", BLOCK_A))
    st = client.get("/epochs/51/status").json()
    assert st["state"] == "finalized"
    assert st["finalized_weight"] == 3
