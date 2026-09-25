"""Registry tests: leases, atomic switch, reference-counted release, rollback."""
import numpy as np
import pytest

from modelswitch.loader import load_candidate
from modelswitch.registry import (
    ModelRegistry,
    NoActiveModelError,
    RollbackUnavailableError,
)

X = np.arange(8, dtype=np.float64) * 0.1


def test_acquire_before_activation_raises():
    with pytest.raises(NoActiveModelError):
        ModelRegistry().acquire()


def test_switch_activates_and_serves(v1_dir):
    registry = ModelRegistry()
    registry.switch(load_candidate(v1_dir))
    assert registry.current_version() == "v1"
    with registry.acquire() as lease:
        assert lease.version == "v1"
        assert lease.model.predict(X).shape == (1, 3)


def test_inflight_request_keeps_old_version_after_switch(v1_dir, v2_dir):
    registry = ModelRegistry()
    v1 = load_candidate(v1_dir)
    registry.switch(v1)

    lease = registry.acquire()  # in-flight request on v1
    registry.switch(load_candidate(v2_dir))

    # v1 is now standby, still loaded, and the in-flight request still works.
    assert not v1.closed
    assert lease.model.predict(X).shape == (1, 3)
    assert registry.current_version() == "v2"
    lease.close()
    assert not v1.closed  # standby is kept loaded for rollback


def test_retired_version_released_only_when_refcount_zero(v1_dir, v2_dir, artifact_factory):
    v3_dir = artifact_factory("v3", seed=303)
    registry = ModelRegistry()
    v1 = load_candidate(v1_dir)
    registry.switch(v1)

    lease = registry.acquire()  # in-flight on v1
    registry.switch(load_candidate(v2_dir))  # v1 -> standby
    registry.switch(load_candidate(v3_dir))  # v1 -> retired, but 1 lease held

    assert not v1.closed, "retired version must survive while a lease is held"
    lease.close()
    assert v1.closed, "retired version must be released once refcount hits zero"


def test_rollback_swaps_active_and_standby(v1_dir, v2_dir):
    registry = ModelRegistry()
    registry.switch(load_candidate(v1_dir))
    registry.switch(load_candidate(v2_dir))
    assert registry.rollback() == "v1"
    assert registry.current_version() == "v1"
    # Rolling back again returns to v2.
    assert registry.rollback() == "v2"


def test_rollback_without_standby_raises(v1_dir):
    registry = ModelRegistry()
    with pytest.raises(RollbackUnavailableError):
        registry.rollback()
    registry.switch(load_candidate(v1_dir))
    with pytest.raises(RollbackUnavailableError):
        registry.rollback()


def test_lease_cannot_be_used_twice(v1_dir):
    registry = ModelRegistry()
    registry.switch(load_candidate(v1_dir))
    lease = registry.acquire()
    lease.close()
    with pytest.raises(RuntimeError, match="already released"):
        _ = lease.model


def test_failed_candidate_never_replaces_active(v1_dir, artifact_factory):
    """A warm-up failure must leave the active version untouched."""
    bad_dir = artifact_factory("v-bad", seed=404, weight_scale=1e308)
    registry = ModelRegistry()
    registry.switch(load_candidate(v1_dir))

    from modelswitch.loader import WarmupError

    with pytest.raises(WarmupError):
        load_candidate(bad_dir)

    assert registry.current_version() == "v1"
    with registry.acquire() as lease:
        assert lease.version == "v1"
        assert np.all(np.isfinite(lease.model.predict(X)))
