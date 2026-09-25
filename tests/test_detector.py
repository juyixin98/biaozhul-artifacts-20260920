"""Tests for the detector core: causality, parity, warm-up, missing data."""

import math
import unittest

import numpy as np

from sudden_anomaly.detector import (
    DECISION_ANOMALY,
    DECISION_NORMAL,
    DECISION_NOT_DECIDED,
    DECISION_WARMING,
    DetectorConfig,
    DetectorConfigError,
    StreamingAnomalyDetector,
    detect_offline,
)


def gaussian(n=2000, seed=7):
    return np.random.default_rng(seed).normal(0.0, 1.0, size=n)


class TestConfigValidation(unittest.TestCase):
    def test_rejects_bad_threshold(self):
        with self.assertRaises(DetectorConfigError):
            DetectorConfig(threshold=0)
        with self.assertRaises(DetectorConfigError):
            DetectorConfig(threshold=float("nan"))

    def test_rejects_inconsistent_history(self):
        with self.assertRaises(DetectorConfigError):
            DetectorConfig(window_size=10, min_history=20)
        with self.assertRaises(DetectorConfigError):
            DetectorConfig(window_size=10, prime_size=4)
        with self.assertRaises(DetectorConfigError):
            DetectorConfig(window_size=10, prime_size=50)

    def test_rejects_bad_estimator(self):
        with self.assertRaises(DetectorConfigError):
            DetectorConfig(scale_estimator="iqr")
        with self.assertRaises(DetectorConfigError):
            DetectorConfig(center="mode")

    def test_rejects_bad_epsilon(self):
        with self.assertRaises(DetectorConfigError):
            DetectorConfig(epsilon_scale=0.0)


class TestWarmup(unittest.TestCase):
    def test_warming_until_prime_then_decided(self):
        cfg = DetectorConfig(window_size=64, min_history=8, prime_size=32, recent_size=32)
        res = detect_offline(gaussian(100), cfg)
        self.assertTrue((res.decisions[:32] == DECISION_WARMING).all())
        self.assertIn(res.decisions[32], (DECISION_NORMAL, DECISION_ANOMALY))
        self.assertTrue(np.isnan(res.scores[:32]).all())
        self.assertFalse(np.isnan(res.scores[32:]).any())

    def test_statistics_use_only_past_samples(self):
        # At the prime index the baseline is built from exactly prime samples.
        cfg = DetectorConfig(window_size=64, prime_size=32, min_history=8, recent_size=32)
        x = gaussian(64)
        res = detect_offline(x, cfg)
        past = x[:32]
        np.testing.assert_allclose(res.centers[32], np.median(past))
        mad = np.median(np.abs(past - np.median(past)))
        np.testing.assert_allclose(res.scales[32], 1.4826 * mad)


class TestCausality(unittest.TestCase):
    def test_prefix_independence_no_future_leakage(self):
        # Decisions on x[:k] must equal first-k decisions on the full signal.
        cfg = DetectorConfig(window_size=128, prime_size=32, min_history=16)
        x = gaussian(500)
        x[300] += 20.0  # huge spike late in the signal
        full = detect_offline(x, cfg)
        for k in (40, 128, 300, 301, 499):
            prefix = detect_offline(x[:k], cfg)
            np.testing.assert_array_equal(prefix.decisions, full.decisions[:k])
            np.testing.assert_array_equal(
                np.nan_to_num(prefix.scores), np.nan_to_num(full.scores[:k])
            )

    def test_spike_does_not_influence_its_own_decision(self):
        cfg = DetectorConfig(window_size=128, prime_size=32, min_history=16)
        x = gaussian(200)
        x[100] += 25.0
        res = detect_offline(x, cfg)
        # The spike is flagged; the following sample's baseline must be
        # virtually unchanged because a robust median/MAD ignores one impulse.
        self.assertEqual(res.decisions[100], DECISION_ANOMALY)
        clean = detect_offline(np.delete(x.copy(), 100)[:200], cfg)
        # compare centers at index 101 computed from windows not yet affected
        # (spike at 100 only enters the window AFTER the decision at 100)
        self.assertAlmostEqual(res.centers[100], clean.centers[100], places=8)


class TestBlockParity(unittest.TestCase):
    def _assert_parity(self, x, cfg=None):
        cfg = cfg or DetectorConfig()
        one_shot = detect_offline(x, cfg)
        for block_size in (1, 2, 7, 100, 257, 10_000):
            blocked = detect_offline(x, cfg, block_size=block_size)
            np.testing.assert_array_equal(
                blocked.decisions, one_shot.decisions,
                err_msg=f"decision mismatch at block_size={block_size}",
            )
            np.testing.assert_array_equal(
                blocked.anomaly_indices, one_shot.anomaly_indices,
                err_msg=f"index mismatch at block_size={block_size}",
            )
            np.testing.assert_allclose(
                np.nan_to_num(blocked.scores), np.nan_to_num(one_shot.scores),
                rtol=0, atol=0, err_msg=f"score mismatch at block_size={block_size}",
            )
            np.testing.assert_array_equal(
                blocked.n_observed, one_shot.n_observed,
                err_msg=f"n_observed mismatch at block_size={block_size}",
            )

    def test_parity_plain_noise(self):
        self._assert_parity(gaussian(1000))

    def test_parity_with_spikes_and_step(self):
        x = gaussian(1500, seed=11)
        x[123] += 15
        x[777] += 15
        x[1000:] += 6
        self._assert_parity(x)

    def test_parity_with_missing(self):
        x = gaussian(800, seed=13)
        x[::37] = np.nan
        x[500] += 12
        self._assert_parity(x)

    def test_manual_point_by_point_matches_batch(self):
        cfg = DetectorConfig(window_size=64, prime_size=16, min_history=8, recent_size=32)
        x = gaussian(300, seed=3)
        batch = StreamingAnomalyDetector(cfg).feed(x)
        det = StreamingAnomalyDetector(cfg)
        point_decisions = []
        for v in x:
            point_decisions.append(det.feed(np.array([v])).decisions[0])
        np.testing.assert_array_equal(np.array(point_decisions), batch.decisions)


class TestMissingSamples(unittest.TestCase):
    def test_nan_is_not_decided_and_imputes_nothing(self):
        cfg = DetectorConfig(window_size=64, prime_size=16, min_history=8, recent_size=32)
        x = gaussian(100)
        x[20] = np.nan
        res = detect_offline(x, cfg)
        self.assertEqual(res.decisions[20], DECISION_NOT_DECIDED)
        self.assertTrue(math.isnan(res.scores[20]))
        self.assertGreater(res.missing_rate, 0)

    def test_gaps_do_not_consume_window(self):
        cfg = DetectorConfig(window_size=64, prime_size=16, min_history=8, recent_size=32)
        x = gaussian(100)
        x[16:48] = np.nan  # 32-sample gap after warm-up
        res = detect_offline(x, cfg)
        # After the gap the observed counter has not advanced during the gap.
        self.assertEqual(res.n_observed[47], res.n_observed[15])
        # Decision resumes immediately on the next observed sample.
        self.assertIn(res.decisions[48], (DECISION_NORMAL, DECISION_ANOMALY))

    def test_nan_at_start_does_not_break_warmup(self):
        cfg = DetectorConfig(window_size=64, prime_size=16, min_history=8, recent_size=32)
        x = gaussian(50)
        x[:5] = np.nan
        res = detect_offline(x, cfg)
        self.assertEqual(res.n_observed[4], 0)
        # Warm-up completes after 16 *observed* samples (index 20 here).
        self.assertEqual(res.decisions[20], DECISION_WARMING)
        self.assertIn(res.decisions[21], (DECISION_NORMAL, DECISION_ANOMALY))

    def test_inf_rejected(self):
        det = StreamingAnomalyDetector(
            DetectorConfig(window_size=64, prime_size=16, min_history=2, recent_size=16)
        )
        det.feed(np.zeros(16))
        with self.assertRaises(ValueError):
            det.feed(np.array([np.inf]))

    def test_missing_sample_resets_shift_streak(self):
        cfg = DetectorConfig(shift_persist=3)
        det = StreamingAnomalyDetector(cfg)
        det.feed(np.zeros(cfg.prime_size))
        det._shift_streak = 2  # simulated two prior exceedances
        det.feed(np.array([np.nan]))
        self.assertEqual(det._shift_streak, 0)

    def test_persist_validation(self):
        with self.assertRaises(DetectorConfigError):
            DetectorConfig(shift_persist=0)


class TestEdgeCases(unittest.TestCase):
    def test_constant_signal_uses_epsilon_floor(self):
        cfg = DetectorConfig(window_size=32, prime_size=16, min_history=8, recent_size=16)
        res = detect_offline(np.zeros(100), cfg)
        self.assertTrue((res.decisions[16:] == DECISION_NORMAL).all())

    def test_jump_from_constant_detected(self):
        cfg = DetectorConfig(
            window_size=64, prime_size=16, min_history=8, threshold=5.0
        )
        x = np.zeros(100)
        x[60:] = 1.0  # any nonzero movement on a constant baseline is anomalous
        res = detect_offline(x, cfg)
        self.assertIn(60, res.anomaly_indices.tolist())

    def test_window_eviction_changes_baseline(self):
        # Window shorter than the post-step region: baseline tracks the new
        # level eventually and anomaly flags stop (adaptation), by design.
        cfg = DetectorConfig(
            window_size=64, prime_size=16, min_history=8, threshold=5.0,
            recent_size=32,
        )
        x = np.zeros(300)
        x[100:] = 3.0
        res = detect_offline(x, cfg)
        self.assertIn(100, res.anomaly_indices.tolist())
        self.assertEqual(res.decisions[-1], DECISION_NORMAL)

    def test_std_estimator_matches_sample_std(self):
        cfg = DetectorConfig(
            window_size=128, prime_size=32, min_history=16,
            scale_estimator="std", center="mean", recent_size=32,
        )
        x = gaussian(100)
        res = detect_offline(x, cfg)
        past = x[:32]
        np.testing.assert_allclose(res.centers[32], past.mean())
        np.testing.assert_allclose(res.scales[32], past.std(ddof=0))


if __name__ == "__main__":
    unittest.main()
