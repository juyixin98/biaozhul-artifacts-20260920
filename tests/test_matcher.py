"""在线有界缓存匹配器的单元测试。

重点：乱序到达、水位线成熟提交、与离线定义的边集一致性、
过期原因、缓存溢出原因、单次消费在流模式下同样成立。
"""

import pytest

from sensor_matcher.matcher import MessageMatcher
from sensor_matcher.models import (
    REASON_BUFFER_OVERFLOW,
    REASON_END_UNMATCHED,
    REASON_EXPIRED,
    Match,
    Message,
)
from sensor_matcher.offline import offline_pair


def msg(mid: str, t: float) -> Message:
    return Message(id=mid, timestamp=t)


def _drain(matcher: MessageMatcher, pushes: list[tuple[str, Message]]) -> None:
    for stream, m in pushes:
        matcher.push(stream, m)
    matcher.flush()


@pytest.mark.unit
def test_ordered_in_order_stream_matches() -> None:
    matcher = MessageMatcher(tolerance=0.05, max_out_of_orderness=0.0)
    pushes = [
        ("a", msg("a1", 0.0)),
        ("b", msg("b1", 0.02)),
        ("a", msg("a2", 1.0)),
        ("b", msg("b2", 1.01)),
    ]
    _drain(matcher, pushes)
    assert {(m.id_a, m.id_b) for m in matcher.matches} == {
        ("a1", "b1"),
        ("a2", "b2"),
    }
    assert matcher.rejects == []


@pytest.mark.unit
def test_out_of_order_arrival_still_matches() -> None:
    # b2(1.01) 先到，随后 a2(1.0)、b1(0.02)、a1(0.0) 乱序到达；
    # 最晚相对偏移为 1.01，故 δ 必须取 1.02 才能保证水位线假设成立。
    matcher = MessageMatcher(tolerance=0.05, max_out_of_orderness=1.02)
    pushes = [
        ("b", msg("b2", 1.01)),
        ("a", msg("a2", 1.0)),
        ("b", msg("b1", 0.02)),
        ("a", msg("a1", 0.0)),
    ]
    _drain(matcher, pushes)
    assert {(m.id_a, m.id_b) for m in matcher.matches} == {
        ("a1", "b1"),
        ("a2", "b2"),
    }


@pytest.mark.unit
def test_late_arriving_closer_candidate_changes_assignment() -> None:
    # 顺序到达时，b1@0.04 先与缓存中的 a1@0 暂存；
    # 更晚（但仍在水位线保证内）到达的 a2@0.05 距 b1 仅 0.01，
    # 最终应按全局边序把 b1 判给 a2（边在 a2 到达前不得提前提交）。
    matcher = MessageMatcher(tolerance=0.1, max_out_of_orderness=0.1)
    pushes = [
        ("a", msg("a1", 0.0)),
        ("b", msg("b1", 0.04)),
        ("a", msg("a2", 0.05)),
        ("a", msg("a3", 0.3)),  # 推进 A 侧观测
        ("b", msg("b3", 0.3)),  # 推进水位线使 0.05 附近成熟
    ]
    _drain(matcher, pushes)
    pairs = {(m.id_a, m.id_b) for m in matcher.matches}
    assert ("a2", "b1") in pairs
    # a1 无其他候选 → 过期/结束未配
    reject_ids = {r.id for r in matcher.rejects if r.id == "a1"}
    assert reject_ids == {"a1"}


@pytest.mark.unit
def test_expired_reason_for_unmatched_old_message() -> None:
    matcher = MessageMatcher(tolerance=0.05, max_out_of_orderness=0.0)
    matcher.push("a", msg("a1", 0.0))
    # 两侧都推进到很远，a1 成熟且无候选
    matcher.push("a", msg("a2", 1.0))
    step = matcher.push("b", msg("b2", 1.0))
    reasons = {r.id: r.reason for r in step.rejects}
    assert reasons.get("a1") == REASON_EXPIRED
    matcher.flush()
    assert any(m.id_a == "a2" and m.id_b == "b2" for m in matcher.matches)


@pytest.mark.unit
def test_end_of_stream_unmatched_reason() -> None:
    # 两侧首条消息互为唯一候选，但时间上不可能成熟（流随即结束），
    # 未配消息在 flush 时标记为 end_of_stream_unmatched。
    matcher = MessageMatcher(tolerance=0.05)
    matcher.push("a", msg("a1", 0.0))
    matcher.push("b", msg("b1", 0.5))
    matcher.flush()
    assert {r.reason for r in matcher.rejects} == {REASON_END_UNMATCHED}
    assert {r.id for r in matcher.rejects} == {"a1", "b1"}


@pytest.mark.unit
def test_buffer_overflow_reason() -> None:
    matcher = MessageMatcher(tolerance=0.05, max_buffer_size=2)
    matcher.push("a", msg("a1", 0.0))
    matcher.push("a", msg("a2", 0.1))
    step = matcher.push("a", msg("a3", 0.2))  # A 缓存满（B 从未到，wm=-inf）
    assert [r.reason for r in step.rejects] == [REASON_BUFFER_OVERFLOW]
    assert [r.id for r in step.rejects] == ["a3"]
    # 被溢出的消息不参与后续配对
    matcher.push("b", msg("b1", 0.0))
    matcher.push("b", msg("b2", 0.1))
    matcher.flush()
    assert {m.id_a for m in matcher.matches} == {"a1", "a2"}


@pytest.mark.unit
def test_single_consumption_holds_online() -> None:
    matcher = MessageMatcher(tolerance=0.05)
    pushes = [
        ("a", msg("a1", 0.0)),
        ("a", msg("a2", 0.02)),
        ("b", msg("b1", 0.0)),
    ]
    _drain(matcher, pushes)
    assert [(m.id_a, m.id_b) for m in matcher.matches] == [("a1", "b1")]
    assert any(r.id == "a2" for r in matcher.rejects)


@pytest.mark.unit
def test_duplicate_timestamp_messages_online() -> None:
    matcher = MessageMatcher(tolerance=0.0)
    pushes = [
        ("a", msg("a1", 1.0)),
        ("a", msg("a1-d1", 1.0)),
        ("b", msg("b1", 1.0)),
        ("b", msg("b2", 1.0)),
    ]
    _drain(matcher, pushes)
    pairs = {(m.id_a, m.id_b) for m in matcher.matches}
    assert pairs == {("a1", "b1"), ("a1-d1", "b2")}


@pytest.mark.unit
def test_ripple_augmenting_chain_does_not_expire_too_early() -> None:
    # 增广链回归：tol=10，顺序 a@0, b@10, a'@10.5 后水位线已使 a@0
    # “成熟”，但该连通分量尚未封闭（b@10 距 wm 容差内仍可能被抢）。
    # 随后 b'@10.6 到达：离线结局应为 a-b、a'-b'，a@0 绝不允许提前过期。
    matcher = MessageMatcher(tolerance=10.0, max_out_of_orderness=0.0)
    matcher.push("a", msg("a0", 0.0))
    matcher.push("b", msg("b10", 10.0))
    matcher.push("a", msg("a105", 10.5))
    # 水位线约 10.5：a0 成熟，但分量含未成熟顶点，不得提交/拒绝
    assert matcher.matches == []
    assert all(r.id != "a0" for r in matcher.rejects)
    matcher.push("b", msg("b106", 10.6))
    # 推进两流到 21，使整个分量封闭
    matcher.push("a", msg("a21", 21.0))
    matcher.push("b", msg("b21", 21.0))
    matcher.flush()
    pairs = {(m.id_a, m.id_b) for m in matcher.matches}
    assert ("a0", "b10") in pairs
    assert ("a105", "b106") in pairs
    assert all(r.id != "a0" for r in matcher.rejects)


@pytest.mark.unit
def test_push_after_flush_raises() -> None:
    matcher = MessageMatcher(tolerance=0.05)
    matcher.push("a", msg("a1", 0.0))
    matcher.flush()
    with pytest.raises(RuntimeError):
        matcher.push("b", msg("b1", 0.0))


@pytest.mark.unit
def test_invalid_arguments() -> None:
    with pytest.raises(ValueError):
        MessageMatcher(tolerance=-0.01)
    with pytest.raises(ValueError):
        MessageMatcher(tolerance=0.05, max_buffer_size=0)
    with pytest.raises(ValueError):
        MessageMatcher(tolerance=0.05, max_out_of_orderness=-1.0)
    matcher = MessageMatcher(tolerance=0.05)
    with pytest.raises(ValueError):
        matcher.push("c", msg("x", 0.0))


@pytest.mark.unit
def test_duplicate_id_push_raises() -> None:
    matcher = MessageMatcher(tolerance=0.05)
    matcher.push("a", msg("a1", 0.0))
    with pytest.raises(ValueError):
        matcher.push("a", msg("a1", 0.5))


@pytest.mark.unit
def test_watermark_requires_both_streams() -> None:
    # 只有 A 流消息时水位线为 -inf，不得有任何成熟提交
    matcher = MessageMatcher(tolerance=0.05)
    step = matcher.push("a", msg("a1", 0.0))
    assert step.matches == ()
    assert step.rejects == ()
    assert step.watermark == float("-inf")


@pytest.mark.unit
@pytest.mark.parametrize("seed", range(8))
def test_online_matches_offline_under_random_shuffles(seed: int) -> None:
    """性质测试：随机时间戳与随机乱序下，无溢出时在线边集 == 离线。"""
    import numpy as np

    rng = np.random.default_rng(seed)
    n = 30
    ta = np.sort(rng.uniform(0.0, 5.0, n))
    tb = np.sort(rng.uniform(0.0, 5.0, n))
    tolerance = 0.08

    a_msgs = [Message(f"a{i}", float(t)) for i, t in enumerate(ta)]
    b_msgs = [Message(f"b{i}", float(t)) for i, t in enumerate(tb)]

    # 时间戳排序后做窗口内随机交换，产生真实的乱序到达序列
    order: list[tuple[str, Message]] = [("a", m) for m in a_msgs]
    order += [("b", m) for m in b_msgs]
    order.sort(key=lambda x: x[1].timestamp)
    for i in range(len(order)):
        j = min(len(order) - 1, i + int(rng.integers(0, 4)))
        order[i], order[j] = order[j], order[i]

    # 从实际到达序列实测乱序有界量 δ（与生产管线的做法一致）
    delta = 0.0
    max_seen = float("-inf")
    for _s, m in order:
        if max_seen != float("-inf"):
            delta = max(delta, max_seen - m.timestamp)
        max_seen = max(max_seen, m.timestamp)
    delta += 1e-9

    matcher = MessageMatcher(
        tolerance=tolerance, max_buffer_size=10_000, max_out_of_orderness=delta
    )
    for stream, m in order:
        matcher.push(stream, m)
    matcher.flush()

    offline = offline_pair(a_msgs, b_msgs, tolerance)
    online_edges = {(m.id_a, m.id_b) for m in matcher.matches}
    offline_edges = {(m.id_a, m.id_b) for m in offline.matches}
    assert online_edges == offline_edges
    assert all(r.reason != REASON_BUFFER_OVERFLOW for r in matcher.rejects)
