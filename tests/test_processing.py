"""逐点 / 逐块 / 独立向量化实现的一致性，以及未来泄漏回归测试。"""

import numpy as np
import pytest

from burst_detector.detector import Decision, DetectorConfig, MissingPolicy
from burst_detector.processing import (
    assert_results_equal,
    detect_blocks,
    detect_signal,
    vectorized_detect,
)
from burst_detector.signal_io import (
    gaussian_noise,
    inject_missing,
    make_scenario,
)


@pytest.mark.parametrize("seed", [0, 7, 123])
@pytest.mark.parametrize("policy", ["skip", "hold"])
def test_point_vs_vectorized_implementations_agree(seed, policy):
    cfg = DetectorConfig(missing_policy=MissingPolicy(policy))
    x = make_scenario("spike", n=600, seed=seed)
    x = inject_missing(x, [50, 51, 300, 599])
    a = detect_signal(x, cfg)
    b = vectorized_detect(x, cfg)
    assert_results_equal(a, b)


@pytest.mark.parametrize("blocks", [
    [2000],
    [1] * 2000,
    [1, 7, 64, 100, 256, 1572],
    [333, 333, 333, 334, 333, 334],
    [500, 1, 499, 1000],
])
def test_block_chunking_matches_pointwise(blocks):
    cfg = DetectorConfig()
    x = make_scenario("step", n=sum(blocks))
    pointwise = detect_signal(x, cfg)
    chunked = detect_blocks(x, cfg, blocks)
    assert_results_equal(pointwise, chunked)


def test_block_chunking_with_missing_samples_skip_policy():
    cfg = DetectorConfig(missing_policy=MissingPolicy.SKIP)
    x = inject_missing(make_scenario("drift", n=800), [0, 1, 400, 799])
    pointwise = detect_signal(x, cfg)
    chunked = detect_blocks(x, cfg, [3, 17, 200, 580])
    assert_results_equal(pointwise, chunked)
    # 前两个样本即缺样：预热被推迟，但状态不因缺样而错乱。
    assert pointwise.decisions[0] is Decision.MISSING
    assert pointwise.decisions[1] is Decision.MISSING


def test_block_sizes_must_cover_signal():
    cfg = DetectorConfig()
    with pytest.raises(ValueError):
        detect_blocks(np.zeros(10), cfg, [4, 4])


def test_no_future_leakage_prefix_invariance():
    """任意前缀的判决，在信号尾部追加任意数据后必须保持不变。"""
    cfg = DetectorConfig(window_size=128, min_samples=20, threshold=6.0)
    x = make_scenario("spike", n=500, seed=3)
    base = detect_signal(x, cfg)
    extended = detect_signal(
        np.concatenate([x, np.full(500, 999.0)]), cfg
    )
    # 判决数组用身份比较（str-Enum 在 NumPy == 下不可靠）。
    assert all(a is b for a, b in zip(base.decisions, extended.decisions[:500]))
    for field_name in ("zscores", "medians", "scales", "n_history"):
        a = getattr(base, field_name)
        b = getattr(extended, field_name)[:500]
        np.testing.assert_allclose(a, b, rtol=1e-12, atol=1e-12,
                                   equal_nan=True)


def test_past_window_excludes_current_sample():
    """历史 [1..5] 后接异常值：当前样本不能进入自身的统计量。"""
    cfg = DetectorConfig(window_size=50, min_samples=5, threshold=6.0)
    x = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 100.0])
    r = detect_signal(x, cfg)
    # 不含当前点：中位数=3；若泄漏则为 [1..5,100] 的中位数 3.5。
    assert r.medians[5] == 3.0
    assert np.isfinite(r.zscores[5])  # min_scale 兜底
    assert r.decisions[5] is Decision.ANOMALY


def test_clean_noise_zero_false_alarms_at_high_threshold():
    cfg = DetectorConfig(threshold=8.0)
    x = gaussian_noise(2000, seed=99)
    r = detect_signal(x, cfg)
    # 阈值 8 个稳健标准差：2000 点高斯序列不应误报。
    assert r.anomaly_indices.size == 0
