"""Tests for synthetic scenarios and evaluation metrics (delay/FAs)."""

import unittest

import numpy as np

from sudden_anomaly.detector import DetectorConfig, detect_offline
from sudden_anomaly.evaluation import evaluate
from sudden_anomaly.signals import (
    make_drift,
    make_missing,
    make_spikes,
    make_step,
)


# Defaults mirror the CLI: two-window causal robust statistics.
CFG = DetectorConfig(
    threshold=5.0, window_size=512, prime_size=64,
    min_history=16, recent_size=32, recent_min=8,
)


class TestStepScenario(unittest.TestCase):
    def test_step_detected_with_small_delay_and_zero_fa_before(self):
        sc = make_step(sample_rate=100.0)
        res = detect_offline(sc.signal, CFG)
        rep = evaluate(sc, res)
        self.assertEqual(rep.detection_rate, 1.0)
        event = rep.events[0]
        self.assertTrue(event.detected)
        # A 6-sigma level jump must be flagged on essentially the first sample.
        self.assertLessEqual(event.delay_samples, 2)
        self.assertAlmostEqual(event.delay_seconds, event.delay_samples / 100.0)
        # No false alarms in clean noise before the step (after warm-up).
        early_fa = [i for i in rep.false_alarm_indices if i < sc.truth[0].start]
        self.assertEqual(early_fa, [])

    def test_small_step_can_be_missed(self):
        # A 1-sigma shift is below threshold: report must honestly say so
        # rather than force a detection.
        sc = make_step(step_amplitude=1.0)
        rep = evaluate(sc, detect_offline(sc.signal, CFG))
        # Either missed or detected late after noise accumulation; both are
        # legitimate, but delay must be reported consistently when detected.
        for e in rep.events:
            if e.detected:
                self.assertGreaterEqual(e.delay_samples, 0)


class TestDriftScenario(unittest.TestCase):
    def test_drift_detected_after_cumulative_offset(self):
        # A purely causal detector cannot see a ramp whose cumulative offset
        # is still within the noise band; the two-window statistic fires once
        # the recent level differs from the older baseline by several group
        # standard errors. Assert detection happens, with a realistic delay
        # band measured for slope=0.004 with the default 512/32 windows.
        sc = make_drift(sample_rate=100.0, drift_slope=0.004)
        rep = evaluate(sc, detect_offline(sc.signal, CFG))
        self.assertEqual(rep.detection_rate, 1.0)
        event = rep.events[0]
        self.assertLess(event.detection_index, sc.n_samples - 400)
        self.assertGreater(event.delay_samples, 0)  # not instantaneous by nature
        self.assertLess(event.delay_samples, 800)


class TestSpikeScenario(unittest.TestCase):
    def test_all_isolated_spikes_detected_at_zero_delay(self):
        sc = make_spikes(spike_indices=[400, 900, 1500], spike_amplitude=12.0)
        res = detect_offline(sc.signal, CFG)
        rep = evaluate(sc, res)
        self.assertEqual(rep.detection_rate, 1.0)
        for event in rep.events:
            self.assertTrue(event.detected)
            self.assertEqual(event.delay_samples, 0)
            self.assertEqual(event.detection_index, event.start)
        # Exactly 3 anomaly decisions: a spike must not smear into neighbours.
        self.assertEqual(rep.n_anomalies, 3)

    def test_spike_indices_match(self):
        sc = make_spikes()
        res = detect_offline(sc.signal, CFG)
        self.assertEqual(sorted(res.anomaly_indices.tolist()), [400, 900, 1500])


class TestMissingDataScenario(unittest.TestCase):
    def test_missing_scenario_still_detects_events(self):
        sc = make_missing(
            make_spikes(spike_indices=[400, 900, 1500]),
            missing_rate=0.05, seed=4242,
        )
        res = detect_offline(sc.signal, CFG)
        rep = evaluate(sc, res)
        # Spike at 400 may itself be dropped (NaN); only count present spikes.
        present = [
            ev.start for ev in sc.truth if not np.isnan(sc.signal[ev.start])
        ]
        detected = {e.start for e in rep.events if e.detected}
        self.assertTrue(set(present) <= detected)
        # Every missing position is NOT_DECIDED, never NORMAL/ANOMALY.
        missing_positions = np.flatnonzero(np.isnan(sc.signal))
        self.assertTrue((res.decisions[missing_positions] == "NOT_DECIDED").all())


class TestFalseAlarmAccounting(unittest.TestCase):
    def test_clean_noise_has_zero_false_alarms(self):
        rng = np.random.default_rng(99)
        from sudden_anomaly.signals import SignalScenario
        x = rng.normal(0.0, 1.0, size=4000)
        sc = SignalScenario("clean", x, 100.0, truth=[])
        rep = evaluate(sc, detect_offline(x, CFG))
        self.assertEqual(rep.n_false_alarms, 0)
        self.assertEqual(rep.detection_rate, 0.0)  # no events -> 0 by convention

    def test_fixed_seed_false_alarm_regression(self):
        # Deterministic long clean record: any detector change that inflates
        # the false-alarm rate trips this test. Seeded -> exactly reproducible.
        x = np.random.default_rng(31337).normal(0.0, 1.0, size=20000)
        res = detect_offline(x, CFG)
        self.assertEqual(res.n_anomalies, 0)
        self.assertEqual(res.anomaly_indices.tolist(), [])


if __name__ == "__main__":
    unittest.main()
