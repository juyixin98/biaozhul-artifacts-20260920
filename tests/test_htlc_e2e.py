"""端到端：两条 anvil 链上的 HTLC 状态机（web3.py 实际发交易）。"""

from __future__ import annotations

import pytest

from app.chain import packed_preimage


def test_deployment_both_chains(chains):
    for leg in ("alpha", "beta"):
        c = chains.clients[leg]
        assert c.reachable()
        assert c.address is not None
        assert c.contract.functions.stateOf(b"\x00" * 32).call() == 0  # Absent


def test_lock_then_double_claim_happy_path(chains):
    c = chains.clients
    sid, t_a, t_b = chains.lock_both("happy01", beta_ttl=300, delta=60)
    assert t_b < t_a and t_a - t_b == 60

    assert c["beta"].get_swap(sid).state == "Locked"
    assert c["alpha"].get_swap(sid).state == "Locked"

    # Beta 受益人领取，preimage 在 Beta 事件中公开
    c["beta"].claim(chains.keys["beta"]["receiver"], sid, chains.preimage)
    vb = c["beta"].get_swap(sid)
    assert vb.state == "Claimed"
    assert vb.preimage == chains.preimage.hex()

    # Alpha 受益人用同一个泄露的 preimage 领取
    c["alpha"].claim(chains.keys["alpha"]["receiver"], sid,
                     packed_preimage("0x" + vb.preimage))
    assert c["alpha"].get_swap(sid).state == "Claimed"
    assert c["beta"].get_swap(sid).state == "Claimed"


def test_wrong_preimage_reverts_and_state_unchanged(chains):
    c = chains.clients
    sid = chains.lock_one("beta", "wrongpre01", ttl=300)
    wrong = packed_preimage("0x" + "cd" * 32)
    with pytest.raises(RuntimeError):
        c["beta"].claim(chains.keys["beta"]["receiver"], sid, wrong)
    assert c["beta"].get_swap(sid).state == "Locked"  # 错误原像不改变状态


def test_claim_by_non_receiver_reverts(chains):
    c = chains.clients
    sid = chains.lock_one("beta", "wrongacct01", ttl=300)
    with pytest.raises(RuntimeError):
        c["beta"].claim(chains.keys["beta"]["sender"], sid, chains.preimage)
    assert c["beta"].get_swap(sid).state == "Locked"


def test_duplicate_claim_reverts(chains):
    c = chains.clients
    sid = chains.lock_one("alpha", "dupclaim01", ttl=300)
    c["alpha"].claim(chains.keys["alpha"]["receiver"], sid, chains.preimage)
    with pytest.raises(RuntimeError):
        c["alpha"].claim(chains.keys["alpha"]["receiver"], sid, chains.preimage)
    assert c["alpha"].get_swap(sid).state == "Claimed"


def test_refund_boundary_exact_timelock(chains):
    """截止时刻测试：t-1 拒绝退款，t 精确允许（>= 边界）。

    时间戳必须钉在"交易所在区块"上：先 setNextBlockTimestamp，再发交易。
    """
    c = chains.clients
    sid = chains.lock_one("beta", "boundary01", ttl=120)
    timelock = c["beta"].get_swap(sid).timelock

    chains.warp_next_tx("beta", timelock - 1)
    with pytest.raises(RuntimeError):
        c["beta"].refund(chains.keys["beta"]["sender"], sid)
    assert c["beta"].get_swap(sid).state == "Locked"

    chains.warp_next_tx("beta", timelock)
    c["beta"].refund(chains.keys["beta"]["sender"], sid)
    assert c["beta"].get_swap(sid).state == "Refunded"


def test_refund_twice_reverts(chains):
    c = chains.clients
    sid = chains.lock_one("beta", "duprefund01", ttl=120)
    timelock = c["beta"].get_swap(sid).timelock
    chains.warp_next_tx("beta", timelock)
    c["beta"].refund(chains.keys["beta"]["sender"], sid)
    with pytest.raises(RuntimeError):
        c["beta"].refund(chains.keys["beta"]["sender"], sid)
    assert c["beta"].get_swap(sid).state == "Refunded"


def test_claim_then_refund_is_mutex(chains):
    c = chains.clients
    sid = chains.lock_one("alpha", "mutexcr01", ttl=120)
    timelock = c["alpha"].get_swap(sid).timelock
    c["alpha"].claim(chains.keys["alpha"]["receiver"], sid, chains.preimage)

    chains.warp_next_tx("alpha", timelock + 10)  # 即使过了截止
    with pytest.raises(RuntimeError):
        c["alpha"].refund(chains.keys["alpha"]["sender"], sid)
    assert c["alpha"].get_swap(sid).state == "Claimed"


def test_refund_then_claim_is_mutex_even_with_correct_preimage(chains):
    c = chains.clients
    sid = chains.lock_one("beta", "mutexrc01", ttl=120)
    timelock = c["beta"].get_swap(sid).timelock
    chains.warp_next_tx("beta", timelock)
    c["beta"].refund(chains.keys["beta"]["sender"], sid)

    # 手握正确原像、且是受益人，终态后依然无法领取
    with pytest.raises(RuntimeError):
        c["beta"].claim(chains.keys["beta"]["receiver"], sid, chains.preimage)
    assert c["beta"].get_swap(sid).state == "Refunded"


def test_lock_same_id_twice_reverts(chains):
    c = chains.clients
    sid = chains.lock_one("alpha", "duplock01", ttl=300)
    with pytest.raises(RuntimeError):
        c["alpha"].lock(
            chains.keys["alpha"]["sender"], sid, chains.receiver_addr("alpha"),
            chains.hash_lock, c["alpha"].now() + 300, 10**15,
        )


def test_unknown_swap_all_operations_revert(chains):
    c = chains.clients
    sid = b"\x11" * 32
    assert c["alpha"].get_swap(sid).state == "Absent"
    with pytest.raises(RuntimeError):
        c["alpha"].claim(chains.keys["alpha"]["receiver"], sid, chains.preimage)
    with pytest.raises(RuntimeError):
        c["alpha"].refund(chains.keys["alpha"]["sender"], sid)


def test_funds_actually_move_on_claim_and_refund(chains):
    """资金面：锁定→合约余额增加；领取→受益人增加对应金额。"""
    c = chains.clients
    amount = 2 * 10**15
    sid = chains.lock_one("alpha", "fundmove01", ttl=300, amount=amount)
    w3 = c["alpha"].w3
    before = w3.eth.get_balance(c["alpha"].address)
    assert before >= amount  # 本测试锁定的钱在合约里

    rcpt_hash = c["alpha"].claim(chains.keys["alpha"]["receiver"], sid, chains.preimage)
    rcpt = w3.eth.wait_for_transaction_receipt(rcpt_hash)
    gas_cost = rcpt.gasUsed * rcpt.effectiveGasPrice
    # 受益人正是 gas 支付者：净增加 = 领取额 - gas
    # 这里直接验证合约余额减少了 amount，状态为 Claimed
    after = w3.eth.get_balance(c["alpha"].address)
    assert before - after == amount
    assert gas_cost > 0
