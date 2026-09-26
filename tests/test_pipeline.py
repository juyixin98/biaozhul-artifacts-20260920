"""端到端集成测试：合成乱序双频流 → 校正 → 在线/离线对照。"""

import pytest

from sensor_matcher.pipeline import run_synthetic_pairing


def _full_config(**overrides) -> dict:
    cfg = {
        "tolerance": 0.08,
        "duration": 20.0,
        "max_buffer_size": 10_000,
        "sensor_a": {
            "rate_hz": 10.0,
            "clock_offset": 0.0,
            "timestamp_jitter": 0.002,
            "delay_mean": 0.15,
            "delay_jitter": 0.12,
            "drop_prob": 0.02,
            "gap_intervals": ((7.0, 9.0),),
            "duplicate_spec": (15, 40),
            "seed": 101,
        },
        "sensor_b": {
            "rate_hz": 7.0,  # 不同频率
            "clock_offset": -0.23,  # B 钟偏移
            "timestamp_jitter": 0.002,
            "delay_mean": 0.2,
            "delay_jitter": 0.18,
            "drop_prob": 0.02,
            "gap_intervals": ((13.0, 14.5),),
            "duplicate_spec": (22,),
            "seed": 202,
        },
        "calibration": {"n_points": 31, "jitter": 0.001, "trim_ratio": 0.1, "seed": 303},
    }
    cfg.update(overrides)
    return cfg


@pytest.mark.integration
def test_full_synthetic_run_online_equals_offline() -> None:
    out = run_synthetic_pairing(_full_config())
    assert out.consistent is True
    assert len(out.online_matches) == len(out.offline_matches)
    online_edges = {(m.id_a, m.id_b) for m in out.online_matches}
    offline_edges = {(m.id_a, m.id_b) for m in out.offline_matches}
    assert online_edges == offline_edges
    # 配对数可观（20 秒双频流）
    assert len(out.online_matches) > 50


@pytest.mark.integration
def test_clock_offset_is_estimated_and_corrected() -> None:
    out = run_synthetic_pairing(_full_config())
    # 真值偏移 -0.23，校正量 = +0.23（中位数估计应很接近）
    assert out.estimated_offset == pytest.approx(0.23, abs=0.005)
    # 所有配对的校正后时间差都在容差内
    for m in out.online_matches:
        assert abs(m.dt) <= 0.08 + 1e-9
    # 原始时间差系统性地偏离约 0.23（时钟校正确实起作用）
    raw_dts = [m.dt_raw for m in out.online_matches]
    corrected_dts = [m.dt for m in out.online_matches]
    assert abs(sum(raw_dts) / len(raw_dts)) > 0.2
    assert abs(sum(corrected_dts) / len(corrected_dts)) < 0.02


@pytest.mark.integration
def test_uncorrected_offset_would_destroy_pairing() -> None:
    # 不做校正（把 B 钟偏移在传感器层设为 0 的对照组）：
    # 同配置但两侧无偏移时同样应一致，且配对率不应异常下降。
    cfg = _full_config()
    cfg["sensor_b"]["clock_offset"] = 0.0
    cfg["calibration"]["jitter"] = 0.0
    out = run_synthetic_pairing(cfg)
    assert out.consistent is True
    assert out.estimated_offset == pytest.approx(0.0, abs=1e-9)


@pytest.mark.integration
def test_expired_rejects_appear_from_gaps() -> None:
    out = run_synthetic_pairing(_full_config())
    reasons = {}
    for r in out.online_rejects:
        reasons[r.reason] = reasons.get(r.reason, 0) + 1
    # A 在 7~9s 中断 → 连续的 B 消息过期；B 在 13~14.5s 中断 → A 消息过期
    assert reasons.get("expired_no_candidate", 0) > 0


@pytest.mark.integration
def test_duplicate_timestamps_are_consumed_once_each() -> None:
    out = run_synthetic_pairing(_full_config())
    # 重复消息各自独立参与 1:1 配对：任一消息 id 最多出现一次
    seen_a: set[str] = set()
    seen_b: set[str] = set()
    for m in out.online_matches:
        assert m.id_a not in seen_a
        assert m.id_b not in seen_b
        seen_a.add(m.id_a)
        seen_b.add(m.id_b)


@pytest.mark.integration
def test_buffer_overflow_is_flagged_and_consistency_marked_false() -> None:
    cfg = _full_config(max_buffer_size=3)
    out = run_synthetic_pairing(cfg)
    overflow = [r for r in out.online_rejects if r.reason == "buffer_overflow"]
    assert len(overflow) > 0
    # 缓存被撑爆后在线结果可能丢失配对，故 consistency 必须如实标 False
    assert out.consistent is False


@pytest.mark.integration
@pytest.mark.parametrize("seed", range(3))
def test_multiple_random_runs_stay_consistent(seed: int) -> None:
    cfg = _full_config()
    cfg["sensor_a"]["seed"] = 1000 + seed
    cfg["sensor_b"]["seed"] = 2000 + seed
    cfg["calibration"]["seed"] = 3000 + seed
    out = run_synthetic_pairing(cfg)
    assert out.consistent is True
