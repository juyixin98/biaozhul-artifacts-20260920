# -*- coding: utf-8 -*-
"""通道四次握手测试：顺序模式、对端、版本绑定；证明与检查点拒绝路径。"""
from __future__ import annotations

import pytest

from app import paths
from app.errors import (
    ERR_INVALID_CHECKPOINT,
    ERR_ORDERING_MISMATCH,
    ERR_PROOF_VALUE,
    ERR_VERSION_MISMATCH,
)
from app.relayer import Relayer


def test_full_four_step_handshake_opens_both_ends(network: Relayer):
    net = network
    net.handshake("chainA", "chainB", ordering="UNORDERED", version="ics20-1")
    a = net.engine.channel_info("chainA", "channel-0")
    b = net.engine.channel_info("chainB", "channel-0")
    assert a["state"] == "OPEN" and b["state"] == "OPEN"
    assert a["counterparty_channel"] == "channel-0"
    assert b["counterparty_channel"] == "channel-0"


def test_ordering_is_bound_and_mismatch_rejected(network: Relayer):
    net = network
    net.engine.channel_open_init("chainA", "channel-0", "conn-1", "ORDERED", "v1", "channel-0")
    net.commit("chainA")
    proof = net.proof("chainA", paths.channel_key("channel-0"))
    with pytest.raises(Exception) as ei:
        net.engine.channel_open_try(
            "chainB", "channel-0", "conn-1", "UNORDERED", "v1", "channel-0", "v1", proof
        )
    assert ei.value.code == ERR_ORDERING_MISMATCH


def test_version_mismatch_rejected_at_try(network: Relayer):
    net = network
    net.engine.channel_open_init("chainA", "channel-0", "conn-1", "UNORDERED", "v1", "channel-0")
    net.commit("chainA")
    proof = net.proof("chainA", paths.channel_key("channel-0"))
    with pytest.raises(Exception) as ei:
        net.engine.channel_open_try(
            "chainB", "channel-0", "conn-1", "UNORDERED", "v2", "channel-0", "v1", proof
        )
    assert ei.value.code == ERR_VERSION_MISMATCH  # v2 != 对端 v1


def test_counterparty_channel_binding_checked(network: Relayer):
    net = network
    net.engine.channel_open_init("chainA", "channel-0", "conn-1", "UNORDERED", "v1", "channel-7")
    net.commit("chainA")
    proof = net.proof("chainA", paths.channel_key("channel-0"))
    # B 端自称 channel-0，但 A 的 INIT 绑定的对端编号是 channel-7
    with pytest.raises(Exception) as ei:
        net.engine.channel_open_try(
            "chainB", "channel-0", "conn-1", "UNORDERED", "v1", "channel-0", "v1", proof
        )
    assert ei.value.code in ("PROOF_KEY_MISMATCH", ERR_PROOF_VALUE)


def test_try_requires_valid_checkpoint_signature(network: Relayer):
    net = network
    net.engine.channel_open_init("chainA", "channel-0", "conn-1", "UNORDERED", "v1", "channel-0")
    net.commit("chainA")
    proof = net.proof("chainA", paths.channel_key("channel-0"))

    # 1) 完全缺失检查点
    no_cp = {k: v for k, v in proof.items() if k != "checkpoint"}
    with pytest.raises(Exception) as ei:
        net.engine.channel_open_try(
            "chainB", "channel-0", "conn-1", "UNORDERED", "v1", "channel-0", "v1", no_cp
        )
    assert ei.value.code == "BAD_PROOF_STRUCTURE"

    # 2) 翻转签名字节 -> 验签失败
    tampered = dict(proof)
    sig = bytearray(bytes.fromhex(proof["checkpoint"]["signature"]))
    sig[0] ^= 0xFF
    cp = dict(proof["checkpoint"])
    cp["signature"] = sig.hex()
    tampered["checkpoint"] = cp
    with pytest.raises(Exception) as ei:
        net.engine.channel_open_try(
            "chainB", "channel-0", "conn-1", "UNORDERED", "v1", "channel-0", "v1", tampered
        )
    assert ei.value.code == ERR_INVALID_CHECKPOINT

    # 3) 用未被信任的第三条链的检查点
    net.engine.create_chain("chainC", revision_number=1, genesis_time_nanos=1_000_000_000_000)
    net.engine.create_client("chainB", "chainC")
    with pytest.raises(Exception) as ei:
        # 把 proof 内容保留，但检查点换成 chainC 同高度的（链编号不符）
        net.engine.channel_open_try(
            "chainB", "channel-0", "conn-1", "UNORDERED", "v1", "channel-0", "v1",
            _forge_chain_id_proof(net, proof, "chainC"),
        )
    assert ei.value.code == ERR_INVALID_CHECKPOINT


def _forge_chain_id_proof(net: Relayer, proof: dict, other_chain: str) -> dict:
    """用 other_chain 的真实私钥给伪造内容签名（证明“正确签名但链不对”也必须拒绝）。"""
    from app import crypto

    cp = dict(proof["checkpoint"])
    cp["chain_id"] = other_chain
    row = net.engine.db.query("SELECT * FROM chains WHERE chain_id=?", (other_chain,))[0]
    sk = crypto.private_key_from_hex(row["privkey_hex"])
    cp["signature"] = crypto.sign(sk, crypto.checkpoint_sign_bytes(
        {k: v for k, v in cp.items() if k != "signature"}
    )).hex()
    out = dict(proof)
    out["checkpoint"] = cp
    return out


def test_stale_proof_state_rejected(network: Relayer):
    """证明锚定的状态键内容与期望不符（例如 A 还在 INIT 却提交 TRYOPEN 证明值）。"""
    net = network
    net.handshake("chainA", "chainB", ordering="UNORDERED", version="v1")
    # B 此时是 OPEN；对 A 再执行一次 Ack，状态必须不是 INIT
    proof = net.proof("chainB", paths.channel_key("channel-0"))
    with pytest.raises(Exception) as ei:
        net.engine.channel_open_ack("chainA", "channel-0", proof)
    assert ei.value.code == "CHANNEL_INVALID_STATE"
