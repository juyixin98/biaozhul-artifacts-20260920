"""Concurrency test: readers must never observe half-loaded weights.

While a switcher thread repeatedly loads candidates (with an artificial
delay injected at every load stage, widening the window in which a broken
implementation could expose a partial model) and atomically switches the
registry, reader threads continuously serve predictions. Every prediction
must be bit-for-bit consistent with exactly one fully loaded version.
"""
import threading
import time

import numpy as np

from modelswitch.loader import load_candidate
from modelswitch.registry import ModelRegistry

X = (np.arange(8, dtype=np.float64) - 3.5) * 0.25
READER_THREADS = 4
SWITCH_ITERATIONS = 12
STAGE_DELAY_SECONDS = 0.002


def _slow_load(path):
    return load_candidate(
        path, on_stage=lambda _stage: time.sleep(STAGE_DELAY_SECONDS)
    )


def test_concurrent_switch_and_read_never_sees_partial_model(v1_dir, v2_dir):
    registry = ModelRegistry()
    registry.switch(load_candidate(v1_dir))

    expected = {
        "v1": load_candidate(v1_dir).predict(X),
        "v2": load_candidate(v2_dir).predict(X),
    }
    stop = threading.Event()
    errors: list[str] = []
    seen_versions: set[str] = set()
    seen_lock = threading.Lock()

    def reader(worker: int) -> None:
        while not stop.is_set():
            try:
                with registry.acquire() as lease:
                    y = lease.model.predict(X)
                    version = lease.version
                if version not in expected:
                    errors.append(f"worker {worker}: unknown version {version!r}")
                    return
                if not np.array_equal(y, expected[version]):
                    errors.append(
                        f"worker {worker}: output inconsistent with {version} "
                        "(half-loaded weights observed)"
                    )
                    return
                with seen_lock:
                    seen_versions.add(version)
            except Exception as exc:  # noqa: BLE001 - record and fail below
                errors.append(f"worker {worker}: {type(exc).__name__}: {exc}")
                return

    def switcher() -> None:
        for i in range(SWITCH_ITERATIONS):
            path = v2_dir if i % 2 == 0 else v1_dir
            registry.switch(_slow_load(path))

    readers = [
        threading.Thread(target=reader, args=(i,), name=f"reader-{i}")
        for i in range(READER_THREADS)
    ]
    for thread in readers:
        thread.start()
    switch_thread = threading.Thread(target=switcher, name="switcher")
    switch_thread.start()
    switch_thread.join()
    stop.set()
    for thread in readers:
        thread.join()

    assert errors == []
    assert seen_versions == {"v1", "v2"}, (
        f"expected traffic on both versions across switches, saw {seen_versions}"
    )
