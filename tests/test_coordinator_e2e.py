"""端到端：只读协调器评估逻辑 + 非原子风险边界场景。"""

from __future__ import annotations

from app.coordinator import assess


def _assess(chains, sid):
    return assess(chains.clients["alpha"], chains.clients["beta"], sid, min_delta=15)


def test_assess_double_lock_convention_ok(chains):
    sid, _, _ = chains.lock_both("assess01", beta_ttl=300, delta=60)
    r = _assess(chains, sid)
    assert r.status == "LOCKED_BOTH"
    assert r.time_plan["deadline_order_ok"] is True
    assert r.time_plan["buffer_ok"] is True
    assert r.time_plan["delta_seconds"] == 60
    assert r.legs["alpha"]["hash_lock"] == r.legs["beta"]["hash_lock"]
    assert r.risk_flags == []


def test_assess_wrong_timelock_order_flagged(chains):
    c = chains.clients
    now = max(c["alpha"].now(), c["beta"].now()) + 1
    # tB > tA：顺序反了
    sid = chains.lock_custom_timelocks("assessbad01", t_b=now + 300, t_a=now + 100)
    r = _assess(chains, sid)
    assert r.time_plan["deadline_order_ok"] is False
    assert any("时间锁顺序错误" in f for f in r.risk_flags)


def test_assess_delta_too_small_flagged(chains):
    sid, _, _ = chains.lock_both("assesssmall01", beta_ttl=300, delta=3)
    r = _assess(chains, sid)
    assert r.time_plan["deadline_order_ok"] is True
    assert r.time_plan["buffer_ok"] is False
    assert any("安全裕度不足" in f for f in r.risk_flags)


def test_assess_one_sided_lock(chains):
    sid = chains.lock_one("alpha", "oneside01", ttl=300)
    r = _assess(chains, sid)
    assert r.status == "ONE_SIDED_LOCK"
    assert any("只有 alpha" in f for f in r.risk_flags)


def test_assess_hash_mismatch(chains):
    sid, _, _ = chains.lock_both("hashmis01", beta_ttl=300, delta=60, same_hash=False)
    r = _assess(chains, sid)
    assert r.status == "HASH_MISMATCH"
    assert r.legs["alpha"]["hash_lock"] != r.legs["beta"]["hash_lock"]


def test_assess_beta_claimed_alpha_pending_exposes_preimage(chains):
    c = chains.clients
    sid, _, _ = chains.lock_both("bpending01", beta_ttl=300, delta=60)
    c["beta"].claim(chains.keys["beta"]["receiver"], sid, chains.preimage)
    r = _assess(chains, sid)
    assert r.status == "BETA_CLAIMED_ALPHA_PENDING"
    assert r.legs["beta"]["preimage"] == "0x" + chains.preimage.hex()
    assert any("立即" in a for a in r.advice)


def test_assess_claimed_both(chains):
    c = chains.clients
    sid, _, _ = chains.lock_both("bothdone01", beta_ttl=300, delta=60)
    c["beta"].claim(chains.keys["beta"]["receiver"], sid, chains.preimage)
    c["alpha"].claim(chains.keys["alpha"]["receiver"], sid, chains.preimage)
    r = _assess(chains, sid)
    assert r.status == "CLAIMED_BOTH"
    assert r.risk_flags == []


def test_assess_refunded_both(chains):
    c = chains.clients
    sid, _, _ = chains.lock_both("bothref01", beta_ttl=120, delta=30)
    t_b = c["beta"].get_swap(sid).timelock
    t_a = c["alpha"].get_swap(sid).timelock
    chains.warp_next_tx("beta", t_b)
    c["beta"].refund(chains.keys["beta"]["sender"], sid)
    chains.warp_next_tx("alpha", t_a)
    c["alpha"].refund(chains.keys["alpha"]["sender"], sid)
    r = _assess(chains, sid)
    assert r.status == "REFUNDED_BOTH"


def test_assess_split_outcome_is_non_atomic(chains):
    """风险边界：Beta 已领取（preimage 公开）+ Alpha 超时退款 = 非原子结局。

    构造方式：Alpha 时间锁极短并直接推进到截止后退款，Beta 保持 Locked 后领取。
    这在真实流程里对应"Beta 领取的消息没能在 Alpha 截止前送达/执行"。
    """
    c = chains.clients
    now = max(c["alpha"].now(), c["beta"].now()) + 1
    # 故意制造 tA 很近、Δ 为负的危险配置（协调器会同时标记顺序错误）
    sid = chains.lock_custom_timelocks("split01", t_b=now + 300, t_a=now + 5)
    t_a = c["alpha"].get_swap(sid).timelock
    chains.warp_next_tx("alpha", t_a)
    c["alpha"].refund(chains.keys["alpha"]["sender"], sid)
    c["beta"].claim(chains.keys["beta"]["receiver"], sid, chains.preimage)

    r = _assess(chains, sid)
    assert r.status == "SPLIT_BETA_CLAIMED_ALPHA_REFUNDED"
    assert any("非原子结果" in f for f in r.risk_flags)


def test_assess_beta_refunded_alpha_locked_warns_not_to_claim(chains):
    c = chains.clients
    sid, _, _ = chains.lock_both("halfref01", beta_ttl=120, delta=60)
    t_b = c["beta"].get_swap(sid).timelock
    chains.warp_next_tx("beta", t_b)
    c["beta"].refund(chains.keys["beta"]["sender"], sid)
    r = _assess(chains, sid)
    assert r.status == "BETA_REFUNDED_ALPHA_LOCKED"
    assert any("领取等于白送" in f for f in r.risk_flags)


def test_assess_chain_pause_is_reported(chains):
    """模拟一链暂停：协调器如实报告不可达，不假装能完成原子交换。"""
    sid, _, _ = chains.lock_both("pause01", beta_ttl=300, delta=60)
    chains.pause("alpha")
    try:
        r = _assess(chains, sid)
        assert r.status == "CHAIN_UNREACHABLE"
        assert r.legs["alpha"]["reachable"] is False
        assert r.legs["beta"]["reachable"] is True
        assert any("alpha" in f and ("不可达" in f or "暂停" in f)
                   for f in r.risk_flags)
    finally:
        chains.resume("alpha")

    # 恢复后状态原样可读
    r = _assess(chains, sid)
    assert r.status == "LOCKED_BOTH"
    assert r.legs["alpha"]["state"] == "Locked"


def test_assess_absent_both(chains):
    sid = b"\xee" * 32
    r = _assess(chains, sid)
    assert r.status == "ABSENT_BOTH"
