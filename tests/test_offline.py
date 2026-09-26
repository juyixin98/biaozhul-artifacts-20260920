"""离线权威定义的单元测试：最近匹配、单次消费、平局规则。"""

import pytest

from sensor_matcher.models import Message
from sensor_matcher.offline import offline_pair


def msg(mid: str, t: float) -> Message:
    return Message(id=mid, timestamp=t)


@pytest.mark.unit
def test_nearest_neighbor_within_tolerance() -> None:
    a = [msg("a1", 0.0), msg("a2", 1.0)]
    b = [msg("b1", 0.04), msg("b2", 0.98)]
    result = offline_pair(a, b, tolerance=0.05)
    assert [(m.id_a, m.id_b) for m in result.matches] == [
        ("a1", "b1"),
        ("a2", "b2"),
    ]
    assert result.unmatched_a == ()
    assert result.unmatched_b == ()


@pytest.mark.unit
def test_outside_tolerance_unmatched() -> None:
    result = offline_pair([msg("a1", 0.0)], [msg("b1", 0.11)], tolerance=0.1)
    assert result.matches == ()
    assert result.unmatched_a == ("a1",)
    assert result.unmatched_b == ("b1",)


@pytest.mark.unit
def test_boundary_distance_is_included() -> None:
    # |dt| == tolerance 是合法候选
    result = offline_pair([msg("a1", 0.0)], [msg("b1", 0.1)], tolerance=0.1)
    assert len(result.matches) == 1


@pytest.mark.unit
def test_single_consumption_one_to_one() -> None:
    # 一个 b 同时是两个 a 的候选，只能被消费一次；最近的 a 胜出，
    # 另一个 a 即便在容差内也不能再配。
    a = [msg("a1", 0.0), msg("a2", 0.02)]
    b = [msg("b1", 0.0)]
    result = offline_pair(a, b, tolerance=0.05)
    assert [(m.id_a, m.id_b) for m in result.matches] == [("a1", "b1")]
    assert result.unmatched_a == ("a2",)


@pytest.mark.unit
def test_tie_earlier_timestamp_wins() -> None:
    # 两条边距离相同：(a1@0, b1@0.05) 与 (a2@0.05, b1@0.05→与 a1 同距)
    # 构造：b1@0.05，a1@0.0 与 a2@0.1，距离都为 0.05；平局按较早时间戳，
    # a1 一端更早 → a1 胜出。
    a = [msg("a1", 0.0), msg("a2", 0.1)]
    b = [msg("b1", 0.05)]
    result = offline_pair(a, b, tolerance=0.1)
    assert [(m.id_a, m.id_b) for m in result.matches] == [("a1", "b1")]
    assert result.unmatched_a == ("a2",)


@pytest.mark.unit
def test_tie_identical_timestamps_resolved_by_id() -> None:
    # 完全同时间戳的重复消息：距离全相同，按 id 字典序确定性消歧
    a = [msg("a-z", 1.0), msg("a-a", 1.0)]
    b = [msg("b2", 1.0), msg("b1", 1.0)]
    result = offline_pair(a, b, tolerance=0.0)
    pairs = {(m.id_a, m.id_b) for m in result.matches}
    assert pairs == {("a-a", "b1"), ("a-z", "b2")}


@pytest.mark.unit
def test_result_independent_of_input_order() -> None:
    a1 = [msg("a1", 0.0), msg("a2", 1.0), msg("a3", 2.0)]
    a2 = list(reversed(a1))
    b1 = [msg("b3", 2.01), msg("b1", 0.02), msg("b2", 0.99)]
    b2 = [msg("b1", 0.02), msg("b3", 2.01), msg("b2", 0.99)]
    r1 = offline_pair(a1, b1, 0.05)
    r2 = offline_pair(a2, b2, 0.05)
    e1 = {(m.id_a, m.id_b) for m in r1.matches}
    e2 = {(m.id_a, m.id_b) for m in r2.matches}
    assert e1 == e2


@pytest.mark.unit
def test_greedy_global_nearest_with_three_chain() -> None:
    # 整数时间戳避免浮点平局歧义：
    # a1@0, a2@5, b1@3, b2@6，容差 10。
    # 边距：a2-b2=1 最短，a2-b1=2 次之（被消费跳过），
    # a1-b1=3 随后成立 → {a2-b2, a1-b1}。
    a = [msg("a1", 0.0), msg("a2", 5.0)]
    b = [msg("b1", 3.0), msg("b2", 6.0)]
    result = offline_pair(a, b, tolerance=10.0)
    pairs = {(m.id_a, m.id_b) for m in result.matches}
    assert pairs == {("a2", "b2"), ("a1", "b1")}


@pytest.mark.unit
def test_tie_between_two_edges_prefers_earlier_pair() -> None:
    # 真正的距离平局（整数）：a2-b1=1（端点 4,5）与 a2-b2=1（端点 5,6），
    # 较早时间戳的边 a2-b1 胜出；a1 随后与 b2 配对。
    a = [msg("a1", 0.0), msg("a2", 5.0)]
    b = [msg("b1", 4.0), msg("b2", 6.0)]
    result = offline_pair(a, b, tolerance=10.0)
    pairs = {(m.id_a, m.id_b) for m in result.matches}
    assert pairs == {("a2", "b1"), ("a1", "b2")}


@pytest.mark.unit
def test_duplicate_ids_rejected() -> None:
    with pytest.raises(ValueError):
        offline_pair([msg("a1", 0.0), msg("a1", 1.0)], [msg("b1", 0.0)], 0.1)


@pytest.mark.unit
def test_negative_tolerance_rejected() -> None:
    with pytest.raises(ValueError):
        offline_pair([msg("a1", 0.0)], [msg("b1", 0.0)], -0.1)


@pytest.mark.unit
def test_empty_streams() -> None:
    result = offline_pair([], [msg("b1", 0.0)], 0.1)
    assert result.matches == ()
    assert result.unmatched_b == ("b1",)
