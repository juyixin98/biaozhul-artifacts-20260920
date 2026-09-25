"""Rollback while reads are in flight.

Timeline under test:

1. v1 active; a request thread acquires a lease on v1 and blocks mid-request.
2. Main thread switches to v2 (v1 -> standby), then rolls back (v1 -> active).
3. The in-flight request must still complete on v1 with valid outputs;
   v1's resources must never have been released.
4. After the request finishes and v1 is eventually retired with refcount
   zero, its resources are released.
"""
import threading

import numpy as np

from modelswitch.loader import load_candidate
from modelswitch.registry import ModelRegistry

X = np.full(8, 0.125, dtype=np.float64)


def test_rollback_during_inflight_read(v1_dir, v2_dir, artifact_factory):
    v3_dir = artifact_factory("v3", seed=303)
    registry = ModelRegistry()
    v1 = load_candidate(v1_dir)
    v2 = load_candidate(v2_dir)
    registry.switch(v1)

    acquired = threading.Event()
    finish = threading.Event()
    result: dict = {}

    def in_flight_request() -> None:
        with registry.acquire() as lease:
            acquired.set()
            finish.wait(timeout=5)
            result["version"] = lease.version
            result["y"] = lease.model.predict(X)

    request_thread = threading.Thread(target=in_flight_request)
    request_thread.start()
    assert acquired.wait(timeout=5)

    registry.switch(v2)          # v1 -> standby (kept loaded for rollback)
    assert not v1.closed
    assert registry.rollback() == "v1"   # v1 active again, v2 -> standby
    assert registry.current_version() == "v1"
    assert not v1.closed

    finish.set()
    request_thread.join(timeout=5)

    # The in-flight request started before the switch and finished after the
    # rollback: it must have been served entirely by v1.
    assert result["version"] == "v1"
    assert np.all(np.isfinite(result["y"]))
    assert np.allclose(result["y"].sum(), 1.0)
    assert not v1.closed  # v1 is active; resources intact

    # Move on: v3 activates, v1 stays standby, v2 retires with zero refs.
    registry.switch(load_candidate(v3_dir))
    assert v2.closed, "standby displaced by a new switch must be released"
    assert not v1.closed

    # Retire v1 as well; with no leases held it is released immediately.
    registry.switch(load_candidate(artifact_factory("v4", seed=404)))
    assert v1.closed, "retired version with refcount zero must be released"
