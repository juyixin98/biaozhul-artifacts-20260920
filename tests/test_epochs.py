"""跨 epoch 隔离：不同 epoch 的投票绝不混算；签名绑定 epoch。"""
from __future__ import annotations

from app import crypto

from .conftest import BLOCK_A, create_epoch, make_validators, post_vote, vote_payload


def test_votes_do_not_cross_epochs(client):
    # 两个 epoch 各 3 名验证者（id 相同、权重相同，但密钥集合独立派生）
    v1, k1 = make_validators([("a", 1), ("b", 1), ("c", 1)])
    v2, k2 = make_validators([("a", 1), ("b", 1), ("c", 1)])
    create_epoch(client, 20, v1)
    create_epoch(client, 21, v2)

    # epoch 20 投 2 票（恰好 2/3，不终局）
    post_vote(client, vote_payload(k1, 20, "a", BLOCK_A))
    post_vote(client, vote_payload(k1, 20, "b", BLOCK_A))
    # epoch 21 投 1 票
    post_vote(client, vote_payload(k2, 21, "a", BLOCK_A))

    s20 = client.get("/epochs/20/status").json()
    s21 = client.get("/epochs/21/status").json()
    assert s20["state"] == "pending" and s20["effective_tally"][0]["weight"] == 2
    assert s21["state"] == "pending" and s21["effective_tally"][0]["weight"] == 1
    # 若跨 epoch 混算，总票 3 会错误终局；这里证明没有

    # 各自补票到终局，互不影响
    post_vote(client, vote_payload(k1, 20, "c", BLOCK_A))
    assert client.get("/epochs/20/status").json()["state"] == "finalized"
    assert client.get("/epochs/21/status").json()["state"] == "pending"


def test_signature_is_epoch_bound(client):
    # 用 epoch 30 的签名消息提交到 epoch 31：验签必须失败
    v, k = make_validators([("a", 1), ("b", 1), ("c", 1)])
    create_epoch(client, 30, v)
    create_epoch(client, 31, v)
    sig_for_30 = crypto.sign_vote(k["a"], 30, BLOCK_A)
    r = post_vote(
        client,
        {
            "epoch": 31,
            "validator_id": "a",
            "block_hash": BLOCK_A,
            "signature": sig_for_30,
        },
    )
    assert r.status_code == 422
    assert "签名" in r.json()["reason"]


def test_unknown_epoch_rejected(client):
    v, k = make_validators([("a", 1)])
    create_epoch(client, 40, v)
    r = post_vote(client, vote_payload(k, 999, "a", BLOCK_A))
    assert r.status_code == 404


def test_unknown_validator_rejected(client):
    v, k = make_validators([("a", 1)])
    create_epoch(client, 41, v)
    _, k_out = make_validators([("outsider", 1)])
    r = post_vote(client, vote_payload(k_out, 41, "outsider", BLOCK_A))
    assert r.status_code == 422
