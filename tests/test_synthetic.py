"""合成数据生成器单元测试。"""

import pytest

from sensor_matcher.synthetic import (
    arrival_order,
    calibration_pairs,
    generate_trajectory,
    make_sensor_events,
    required_out_of_orderness,
)


@pytest.mark.unit
def test_trajectory_interpolation() -> None:
    traj = generate_trajectory(duration=2.0, dt=0.01, speed=1.0, lateral_amp=0.0)
    pos = traj.position_at(0.5)
    assert pos[0] == pytest.approx(0.5)
    assert pos[1] == pytest.approx(0.0, abs=1e-12)


@pytest.mark.unit
def test_sensor_rates_and_clock_offset() -> None:
    traj = generate_trajectory(duration=1.0, dt=0.001)
    events = make_sensor_events(
        "a", traj, rate_hz=10.0, clock_offset=0.3, delay_mean=0.0, seed=1
    )
    # 1 秒内约 11 个采样点（含 t=0 与 t=1.0）
    assert 10 <= len(events) <= 11
    # 原始时间戳 = 真值 + 0.3（无 jitter）
    for ev in events:
        assert ev.message.timestamp == pytest.approx(ev.true_time + 0.3, abs=1e-12)


@pytest.mark.unit
def test_duplicate_spec_shares_timestamp_and_seq() -> None:
    traj = generate_trajectory(duration=1.0)
    events = make_sensor_events(
        "b", traj, rate_hz=5.0, duplicate_spec=(2,), delay_mean=0.0, seed=2
    )
    ids = [ev.message.id for ev in events]
    assert "b0002" in ids and "b0002-d1" in ids
    dup = [ev for ev in events if ev.message.id in ("b0002", "b0002-d1")]
    assert dup[0].message.timestamp == dup[1].message.timestamp
    assert dup[0].true_time == dup[1].true_time


@pytest.mark.unit
def test_gap_intervals_produce_expiry() -> None:
    traj = generate_trajectory(duration=10.0)
    # A 在 2~6 秒中断；B 连续，B 在该区间内的消息将无法匹配
    events_a = make_sensor_events(
        "a", traj, rate_hz=2.0, gap_intervals=((2.0, 6.0),),
        delay_mean=0.0, seed=3,
    )
    events_b = make_sensor_events("b", traj, rate_hz=2.0, delay_mean=0.0, seed=4)
    gap_a_times = [ev.true_time for ev in events_a if 2.0 <= ev.true_time < 6.0]
    assert gap_a_times == []
    assert len(events_b) > len(events_a)


@pytest.mark.unit
def test_drop_prob_reduces_count() -> None:
    traj = generate_trajectory(duration=10.0)
    full = make_sensor_events("a", traj, rate_hz=10.0, drop_prob=0.0, seed=5)
    dropped = make_sensor_events("a", traj, rate_hz=10.0, drop_prob=0.5, seed=5)
    assert len(dropped) < len(full)


@pytest.mark.unit
def test_out_of_order_arrival_generated() -> None:
    traj = generate_trajectory(duration=5.0)
    events_a = make_sensor_events(
        "a", traj, rate_hz=10.0, delay_mean=0.1, delay_jitter=0.1, seed=6
    )
    events_b = make_sensor_events(
        "b", traj, rate_hz=8.0, delay_mean=0.1, delay_jitter=0.1, seed=7
    )
    order = arrival_order(events_a, events_b)
    stamps = [ev.message.timestamp for _s, ev in order]
    assert stamps != sorted(stamps)  # 确实存在乱序
    delta = required_out_of_orderness(events_a, events_b)
    assert delta > 0.0
    # δ 是有效上界
    max_seen = float("-inf")
    for t in stamps:
        if max_seen != float("-inf"):
            assert max_seen - t <= delta + 1e-9
        max_seen = max(max_seen, t)


@pytest.mark.unit
def test_calibration_pairs_estimate() -> None:
    traj = generate_trajectory(duration=10.0)
    a, b = calibration_pairs(traj, n_points=51, clock_offset_b=-0.15, seed=8)
    from sensor_matcher.clock import estimate_offset

    corr = estimate_offset(a, b)
    assert corr.offset == pytest.approx(0.15, abs=0.003)


@pytest.mark.unit
def test_invalid_sensor_arguments() -> None:
    traj = generate_trajectory(duration=1.0)
    with pytest.raises(ValueError):
        make_sensor_events("x", traj, rate_hz=10.0)
    with pytest.raises(ValueError):
        make_sensor_events("a", traj, rate_hz=0.0)
    with pytest.raises(ValueError):
        make_sensor_events("a", traj, rate_hz=10.0, drop_prob=1.0)
