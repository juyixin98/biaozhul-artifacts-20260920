"""检测器单元测试：因果性、初始化、缺样策略、去抖。"""

import numpy as np
import pytest

from burst_detector.detector import (
    BurstAnomalyDetector,
    Decision,
    DetectorConfig,
    MissingPolicy,
)


def make_detector(**kw):
    return BurstAnomalyDetector(DetectorConfig(**kw))


def test_warmup_blocks_all_decisions():
    det = make_detector(min_samples=10, window_size=50)
    for i in range(9):
        r = det.update(0.0, index=i)
        assert r.decision is Decision.WARMUP
    # 第 10 个有效历史之后，样本才进入正常判决。
    r = det.update(0.0, index=9)
    assert r.decision is Decision.WARMUP  # 判决时历史只有 9 个
    r = det.update(0.0, index=10)
    assert r.decision is Decision.NORMAL  # 判决时历史 10 个


def test_threshold_uses_only_past_samples_no_future_leak():
    """关键性质：同一历史前缀下，判决不随未来样本变化。"""
    cfg = DetectorConfig(min_samples=5, window_size=50, threshold=6.0)
    prefix = np.array([0.0, 0.01, -0.02, 0.0, 0.01, 5.0])

    det1 = make_detector(min_samples=5, window_size=50, threshold=6.0)
    for i, v in enumerate(prefix):
        r1 = det1.update(v, index=i)

    # 用完全相同的前缀，但“未来”放一个巨大异常；当前判决必须一致。
    det2 = make_detector(min_samples=5, window_size=50, threshold=6.0)
    r2 = None
    for i, v in enumerate(prefix):
        r2 = det2.update(v, index=i)
    det2.update(10_000.0)  # 未来的尖峰
    assert r1.decision == r2.decision
    assert r1.zscore == r2.zscore

    # 直接验证：判决时的历史只包含严格过去样本。
    det = make_detector(min_samples=5, window_size=50)
    past = [1.0, 2.0, 3.0, 4.0, 5.0]
    for v in past:
        det.update(v)
    r = det.update(100.0)
    assert r.n_history == 5
    # 若当前点泄漏进窗口，6 个值 [1..5,100] 的中位数会是 3.5 而非 3。
    assert r.median == 3.0
    assert np.isfinite(r.zscore)
    assert r.decision is Decision.ANOMALY


def test_isolated_spike_detected_immediately():
    det = make_detector(min_samples=30, window_size=200, threshold=6.0)
    rng = np.random.default_rng(1)
    noise = rng.standard_normal(100)
    last = None
    for i, v in enumerate(noise):
        last = det.update(v, index=i)
        assert last.decision is not Decision.ANOMALY
    spike_idx = 100
    r = det.update(50.0, index=spike_idx)
    assert r.decision is Decision.ANOMALY
    # 单点尖峰：延迟 0。
    assert r.index == spike_idx


def test_min_duration_debounce():
    # min_duration=3：需要连续 3 点越限才确认。
    det = make_detector(min_samples=10, window_size=100,
                        threshold=6.0, min_duration=3)
    for v in np.zeros(10):
        det.update(v)
    r1 = det.update(100.0)
    r2 = det.update(100.0)
    r3 = det.update(100.0)
    assert r1.decision is Decision.NORMAL
    assert r2.decision is Decision.NORMAL
    assert r3.decision is Decision.ANOMALY
    # 越限中断后计数清零。
    det.update(0.0)
    r = det.update(100.0)
    assert r.decision is Decision.NORMAL


def test_missing_skip_policy_leaves_state_untouched():
    det = make_detector(min_samples=5, window_size=50,
                        missing_policy=MissingPolicy.SKIP)
    for v in [1.0, 1.0, 1.0, 1.0, 1.0]:
        det.update(v)
    n_before = det.n_history
    r = det.update(np.nan)
    assert r.decision is Decision.MISSING
    assert det.n_history == n_before  # 缺样不入窗
    assert np.isnan(r.value) and np.isnan(r.zscore)
    # 连续缺样后正常样本仍可判决，状态连续。
    r = det.update(1.0)
    assert r.decision is Decision.NORMAL
    assert det.n_history == n_before + 1


def test_missing_hold_policy_fills_with_last_valid():
    det = make_detector(min_samples=5, window_size=50,
                        missing_policy=MissingPolicy.HOLD)
    for v in [1.0, 1.0, 1.0, 1.0, 1.0]:
        det.update(v)
    r = det.update(np.nan)
    assert r.decision is not Decision.MISSING
    assert r.value == 1.0  # 用上一有效观测填补
    # HOLD 在无任何有效历史时退化为 MISSING。
    fresh = make_detector(min_samples=5, missing_policy=MissingPolicy.HOLD)
    r0 = fresh.update(np.nan)
    assert r0.decision is Decision.MISSING


def test_inf_treated_as_missing():
    det = make_detector(min_samples=2)
    det.update(0.0)
    det.update(0.0)
    r = det.update(np.inf)
    assert r.decision is Decision.MISSING


def test_config_validation():
    with pytest.raises(ValueError):
        DetectorConfig(window_size=0)
    with pytest.raises(ValueError):
        DetectorConfig(threshold=-1)
    with pytest.raises(ValueError):
        DetectorConfig(min_samples=1000, window_size=10)
    with pytest.raises(ValueError):
        DetectorConfig(min_duration=0)


def test_string_missing_policy_accepted():
    cfg = DetectorConfig(missing_policy="hold")
    assert cfg.missing_policy is MissingPolicy.HOLD


def test_reset_restores_fresh_state():
    det = make_detector(min_samples=5)
    for v in np.zeros(10):
        det.update(v)
    assert det.n_history > 0
    det.reset()
    assert det.n_history == 0
