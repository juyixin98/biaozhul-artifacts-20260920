"""阈值边界：少一票不终局、恰好 2/3 不终局、严格超过才终局（纯整数）。"""
from __future__ import annotations

from app.finality import is_strict_supermajority, required_weight

from .conftest import BLOCK_A, create_epoch, make_validators, post_vote, vote_payload


def test_integer_supermajority_math():
    # 3w > 2W 的精确边界
    assert not is_strict_supermajority(2, 3)   # 恰好 2/3
    assert is_strict_supermajority(3, 3)
    assert not is_strict_supermajority(6, 9)
    assert is_strict_supermajority(7, 9)
    assert not is_strict_supermajority(66, 99)
    assert is_strict_supermajority(67, 99)
    assert not is_strict_supermajority(0, 0)   # 零总权重永不能终局
    assert not is_strict_supermajority(5, 0)
    assert required_weight(3) == 3
    assert required_weight(9) == 7
    assert required_weight(99) == 67
    assert required_weight(0) == 0


def test_exactly_two_thirds_does_not_finalize(client):
    # 3 个验证者各权重 1：2/3 票 == 恰好 2/3，不终局；第 3 票才终局
    validators, keys = make_validators([("v1", 1), ("v2", 1), ("v3", 1)])
    create_epoch(client, 1, validators)

    post_vote(client, vote_payload(keys, 1, "v1", BLOCK_A))
    r = post_vote(client, vote_payload(keys, 1, "v2", BLOCK_A))
    assert r.status_code == 200
    st = client.get("/epochs/1/status").json()
    assert st["state"] == "pending"
    assert st["finalized_block"] is None
    assert st["effective_tally"][0]["weight"] == 2  # 恰好 2/3，不够

    r = post_vote(client, vote_payload(keys, 1, "v3", BLOCK_A))
    assert r.json()["epoch_state"] == "finalized"
    st = client.get("/epochs/1/status").json()
    assert st["state"] == "finalized"
    assert st["finalized_block"] == BLOCK_A
    assert st["finalized_weight"] == 3


def test_one_vote_short_does_not_finalize(client):
    # 权重 3/3/3，总 9，需 >6 即 >=7；6 票（两验证者）不够
    validators, keys = make_validators([("a", 3), ("b", 3), ("c", 3)])
    create_epoch(client, 2, validators)

    post_vote(client, vote_payload(keys, 2, "a", BLOCK_A))
    post_vote(client, vote_payload(keys, 2, "b", BLOCK_A))
    st = client.get("/epochs/2/status").json()
    assert st["state"] == "pending"
    assert st["effective_tally"][0]["weight"] == 6  # 6*3 = 18 不 > 9*2 = 18

    post_vote(client, vote_payload(keys, 2, "c", BLOCK_A))
    st = client.get("/epochs/2/status").json()
    assert st["state"] == "finalized"
    assert st["finalized_weight"] == 9


def test_weighted_threshold_boundary(client):
    # 权重 10/10/10/1，总 31，需 >62/3 即 >=21；20 不够，21 够
    validators, keys = make_validators(
        [("w1", 10), ("w2", 10), ("w3", 10), ("w4", 1)]
    )
    create_epoch_resp = client.post(
        "/epochs", json={"epoch": 3, "validators": validators}
    )
    assert create_epoch_resp.status_code == 201

    post_vote(client, vote_payload(keys, 3, "w1", BLOCK_A))
    post_vote(client, vote_payload(keys, 3, "w2", BLOCK_A))
    st = client.get("/epochs/3/status").json()
    assert st["state"] == "pending"  # 20 < 21

    post_vote(client, vote_payload(keys, 3, "w4", BLOCK_A))  # +1 = 21
    st = client.get("/epochs/3/status").json()
    assert st["state"] == "finalized"
    assert st["finalized_weight"] == 21
