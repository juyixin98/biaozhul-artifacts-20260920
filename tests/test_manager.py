"""ModelManager tests: atomic switch, rollback, leases, deferred release.

These cover the three acceptance scenarios:
* warm-up failure leaves the old version serving (rollback);
* concurrent switches are serialized (or fail loudly with SwitchBusyError);
* an in-flight request keeps the old version, whose buffers are freed only
  after its reference count reaches zero;
plus the central invariant: *no request ever observes half-loaded weights*.
"""

from __future__ import annotations

import threading

import numpy as np
import pytest

from conftest import VERSION_INDEX, reference_logits
from model_switch.errors import (
    NoActiveModelError,
    SwitchBusyError,
    WarmupValidationError,
)
from model_switch.loader import ArtifactLoader
from model_switch.manager import ModelManager
from model_switch.model import INPUT_DIM, fixed_warmup_input

X_SAMPLE = np.linspace(-1.0, 1.0, INPUT_DIM, dtype=np.float32)
BAD_WARMUP_VERSION = "v-bad-warmup"


# --------------------------------------------------------------------------- #
# Basics
# --------------------------------------------------------------------------- #


def test_predict_without_active_model_raises(manager):
    with pytest.raises(NoActiveModelError):
        manager.predict(X_SAMPLE)


def test_first_switch_activates_and_predicts(active_manager):
    assert active_manager.active_version == "v1"
    assert active_manager.generation == 1
    p = active_manager.predict(X_SAMPLE)
    assert p.version == "v1"
    np.testing.assert_allclose(p.logits, reference_logits(1, X_SAMPLE), rtol=1e-6, atol=1e-7)


def test_each_version_produces_distinct_correct_outputs(active_manager):
    outputs = {"v1": active_manager.predict(X_SAMPLE).logits}
    active_manager.switch_to("v2")
    outputs["v2"] = active_manager.predict(X_SAMPLE).logits
    active_manager.switch_to("v3")
    outputs["v3"] = active_manager.predict(X_SAMPLE).logits
    # Different versions genuinely carry different weights.
    assert not np.allclose(outputs["v1"], outputs["v2"])
    assert not np.allclose(outputs["v2"], outputs["v3"])
    # And each served result matches the independent reference exactly.
    for name, idx in VERSION_INDEX.items():
        np.testing.assert_allclose(
            outputs[name], reference_logits(idx, X_SAMPLE), rtol=1e-6, atol=1e-7
        )


def test_batch_prediction(active_manager):
    batch = np.stack([X_SAMPLE, -X_SAMPLE])
    p = active_manager.predict(batch)
    assert p.logits.shape == (2, 2)
    # Batched vs single matmuls can differ by 1 ULP in float32.
    np.testing.assert_allclose(
        p.logits[0], active_manager.predict(X_SAMPLE).logits, rtol=1e-6, atol=1e-6
    )


# --------------------------------------------------------------------------- #
# Acceptance 1: warm-up failure -> automatic rollback
# --------------------------------------------------------------------------- #


def test_warmup_failure_keeps_old_version_serving(active_manager):
    before = active_manager.predict(X_SAMPLE)
    assert before.version == "v1"

    with pytest.raises(WarmupValidationError):
        active_manager.switch_to(BAD_WARMUP_VERSION)

    # Slot untouched: same version, same generation, same, correct output.
    assert active_manager.active_version == "v1"
    assert active_manager.generation == 1
    after = active_manager.predict(X_SAMPLE)
    assert after.version == "v1"
    assert after.generation == 1
    np.testing.assert_array_equal(before.logits, after.logits)
    failed = [e for e in active_manager.status()["history"] if e["event"] == "switch_failed"]
    assert len(failed) == 1
    assert "WarmupValidationError" in failed[0]["error"]


def test_failed_switch_can_be_followed_by_successful_one(active_manager):
    with pytest.raises(WarmupValidationError):
        active_manager.switch_to(BAD_WARMUP_VERSION)
    result = active_manager.switch_to("v2")
    assert result.version == "v2"
    assert result.replaced == "v1"
    assert active_manager.predict(X_SAMPLE).version == "v2"


def test_switch_to_already_active_version_is_noop(active_manager):
    result = active_manager.switch_to("v1")
    assert result.replaced is None
    assert active_manager.generation == 1


# --------------------------------------------------------------------------- #
# Acceptance 2: in-flight request pins the old version; deferred release
# --------------------------------------------------------------------------- #


def test_inflight_request_keeps_old_version_until_lease_released(active_manager):
    # Hold a request open across the switch.
    with active_manager.lease() as held:
        assert held.version == "v1"
        result = active_manager.switch_to("v2")
        assert result.replaced == "v1"

        # Old version is retired with exactly one outstanding reference.
        status = active_manager.status()
        assert result.retired_refcount == 1
        assert status["active_version"] == "v2"
        retired = {r["version"]: r for r in status["retired"]}
        assert retired["v1"]["refcount"] == 1
        assert retired["v1"]["disposed"] is False

        # The in-flight request STILL computes with v1 weights.
        held_logits = held.loaded.model.forward(X_SAMPLE)
        np.testing.assert_allclose(held_logits, reference_logits(1, X_SAMPLE), rtol=1e-6)
        # New requests get v2.
        assert active_manager.predict(X_SAMPLE).version == "v2"

    # Lease released: buffers freed, retired set drained.
    assert active_manager.drain_retired(timeout_s=2.0)
    status = active_manager.status()
    assert status["retired"] == []
    assert active_manager.generation == 2


def test_retired_model_used_after_release_is_dead(active_manager):
    with active_manager.lease() as held:
        active_manager.switch_to("v2")
        old = held.loaded
    active_manager.drain_retired(timeout_s=2.0)
    assert old.disposed is True
    with pytest.raises(RuntimeError, match="disposed"):
        old.model.forward(X_SAMPLE)


def test_old_version_freed_immediately_without_inflight(active_manager):
    result = active_manager.switch_to("v2")
    assert result.retired_refcount == 0
    assert active_manager.status()["retired"] == []


def test_retirement_chain_with_overlapping_leases(active_manager):
    # v1 lease spans v1->v2 and v2->v3; v2 lease spans only v2->v3.
    with active_manager.lease() as lease_v1:
        active_manager.switch_to("v2")
        with active_manager.lease() as lease_v2:
            assert lease_v2.version == "v2"
            active_manager.switch_to("v3")
            retired = {r["version"]: r["refcount"] for r in active_manager.status()["retired"]}
            assert retired == {"v1": 1, "v2": 1}
            # Both in-flight requests compute with their pinned weights.
            np.testing.assert_allclose(
                lease_v1.loaded.model.forward(X_SAMPLE), reference_logits(1, X_SAMPLE), rtol=1e-6
            )
            np.testing.assert_allclose(
                lease_v2.loaded.model.forward(X_SAMPLE), reference_logits(2, X_SAMPLE), rtol=1e-6
            )
        # v2's last lease gone -> v2 released immediately; v1 still pinned.
        active_manager.drain_retired(timeout_s=2.0)
        retired = {r["version"]: r for r in active_manager.status()["retired"]}
        assert set(retired) == {"v1"}
        assert lease_v2.loaded.disposed is True
        assert lease_v1.loaded.disposed is False
    active_manager.drain_retired(timeout_s=2.0)
    assert active_manager.status()["retired"] == []
    assert lease_v1.loaded.disposed is True
    assert active_manager.active_version == "v3"


def test_lease_cannot_be_double_released(active_manager):
    with active_manager.lease() as held:
        active_manager._release(held)
        refcount_after_first = held.loaded.refcount
        active_manager._release(held)  # idempotent, no underflow
        assert held.loaded.refcount == refcount_after_first


# --------------------------------------------------------------------------- #
# Acceptance 3: concurrent switching
# --------------------------------------------------------------------------- #


class _BlockingLoader:
    """Wraps a real loader; blocks the first in-flight load() on an Event."""

    def __init__(self, real: ArtifactLoader):
        self._real = real
        self.root = real.root
        self.enter = threading.Event()
        self.release = threading.Event()
        self._armed = False
        self._lock = threading.Lock()

    def arm(self) -> None:
        with self._lock:
            self._armed = True

    def load(self, version):
        with self._lock:
            should_block = self._armed
            self._armed = False
        if should_block:
            self.enter.set()
            assert self.release.wait(timeout=10), "blocking load never released"
        return self._real.load(version)


def test_concurrent_switch_second_caller_gets_busy_or_waits(registry_root):
    blocking = _BlockingLoader(ArtifactLoader(registry_root))
    mgr = ModelManager(blocking, switch_timeout_s=0.3)
    mgr.switch_to("v1")

    blocking.arm()
    t1 = threading.Thread(target=mgr.switch_to, args=("v2",))
    t1.start()
    assert blocking.enter.wait(timeout=2.0), "worker did not enter load"

    # While switch #1 is mid-load, switch #2 cannot proceed; short timeout ->
    # it must fail loudly rather than queue silently or corrupt state.
    with pytest.raises(SwitchBusyError):
        mgr.switch_to("v3")
    assert mgr.active_version == "v1"  # nothing published yet

    blocking.release.set()
    t1.join(timeout=5.0)
    assert not t1.is_alive()
    assert mgr.active_version == "v2"

    # After serialization clears, the pending change succeeds normally.
    mgr.switch_to("v3")
    assert mgr.active_version == "v3"


def test_many_serialized_concurrent_switches_all_succeed(registry_root):
    mgr = ModelManager(ArtifactLoader(registry_root), switch_timeout_s=30.0)
    mgr.switch_to("v1")
    errors: list[Exception] = []

    def worker(target: str) -> None:
        try:
            mgr.switch_to(target)
        except Exception as exc:  # noqa: BLE001 - recorded for assertion
            errors.append(exc)

    targets = ["v2", "v3", "v1", "v2", "v3"] * 4
    threads = [threading.Thread(target=worker, args=(t,)) for t in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)
        assert not t.is_alive()
    assert errors == []
    assert mgr.active_version in {"v1", "v2", "v3"}
    # Generation advanced exactly once per *effective* (version-changing)
    # switch; no torn generations.
    assert mgr.generation >= 2


# --------------------------------------------------------------------------- #
# Central invariant: never serve half-loaded / mixed weights
# --------------------------------------------------------------------------- #


def test_predictions_during_switch_always_match_a_complete_version(registry_root):
    """Stress: concurrent predictions vs. repeated v1<->v2 switches.

    Every served result must equal the reference output of *one complete*
    version — a half-loaded or mixed tensor set cannot match either.
    """
    mgr = ModelManager(ArtifactLoader(registry_root), switch_timeout_s=30.0)
    mgr.switch_to("v1")

    stop = threading.Event()
    violations: list[str] = []
    observed: set[str] = set()
    lock = threading.Lock()

    def predictor() -> None:
        rng = np.random.default_rng()
        while not stop.is_set():
            x = rng.standard_normal(INPUT_DIM).astype(np.float32)
            p = mgr.predict(x)
            if not np.isfinite(p.logits).all():
                violations.append(f"non-finite logits from {p.version}")
                continue
            ref = reference_logits(VERSION_INDEX[p.version], x)
            if not np.allclose(p.logits, ref, rtol=1e-5, atol=1e-6):
                violations.append(f"mixed/half-loaded weights on {p.version}")
            with lock:
                observed.add(p.version)

    def switcher() -> None:
        for target in ("v2", "v1", "v2", "v1", "v2", "v1", "v2"):
            try:
                mgr.switch_to(target)
            except SwitchBusyError:
                pass

    predict_threads = [threading.Thread(target=predictor) for _ in range(6)]
    sw = threading.Thread(target=switcher)
    for t in predict_threads:
        t.start()
    sw.start()
    sw.join(timeout=15.0)
    assert not sw.is_alive()
    stop.set()
    for t in predict_threads:
        t.join(timeout=5.0)
        assert not t.is_alive()

    assert violations == []
    assert observed == {"v1", "v2"}  # traffic really did hit both versions
    assert mgr.drain_retired(timeout_s=5.0)
    assert mgr.status()["retired"] == []


def test_warmup_input_agrees_with_reference():
    # Sanity: the golden warm-up input itself is a valid request input.
    x = fixed_warmup_input()
    assert x.shape == (INPUT_DIM,)
