"""零权重验证者：计入总权重、其票不计权、不能帮助终局。"""
from __future__ import annotations

from .conftest import BLOCK_A, create_epoch, make_validators, post_vote, vote_payload


def test_zero_weight_validator_cannot_finalize(client):
    # v1=10, v2=10, z=0：总 20，需 >40/3 即 >=14
    validators, keys = make_validators([("v1", 10), ("v2", 10), ("z", 0)])
    create_epoch(client, 10, validators)

    r = post_vote(client, vote_payload(keys, 10, "z", BLOCK_A))
    assert r.status_code == 200  # 零权重票合法接收，但不计权
    st = client.get("/epochs/10/status").json()
    assert st["effective_tally"][0]["weight"] == 0
    assert st["state"] == "pending"

    post_vote(client, vote_payload(keys, 10, "v1", BLOCK_A))
    st = client.get("/epochs/10/status").json()
    assert st["state"] == "pending"  # 10 < 14

    post_vote(client, vote_payload(keys, 10, "v2", BLOCK_A))
    st = client.get("/epochs/10/status").json()
    assert st["state"] == "finalized"
    assert st["finalized_weight"] == 20


def test_all_zero_weight_epoch_never_finalizes(client):
    validators, keys = make_validators([("z1", 0), ("z2", 0)])
    create_epoch(client, 11, validators)
    post_vote(client, vote_payload(keys, 11, "z1", BLOCK_A))
    post_vote(client, vote_payload(keys, 11, "z2", BLOCK_A))
    st = client.get("/epochs/11/status").json()
    assert st["total_weight"] == 0
    assert st["state"] == "pending"
    assert st["finalized_block"] is None
