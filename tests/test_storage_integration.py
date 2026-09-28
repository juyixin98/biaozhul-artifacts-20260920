"""Integration tests for SQLite storage: idempotency, concurrency, recovery."""
from __future__ import annotations

import threading

import pytest

from stratblock.contract import (
    AllocationError,
    ErrorCategory,
    build_study_config,
)
from stratblock.rng import draw_next
from stratblock.storage import Storage


def _enroll(store, cfg, seed, subject, features, request_id, actor="enroller"):
    return store.enroll(cfg, seed, subject, features, request_id, actor, draw_next)


@pytest.mark.integration
def test_repeat_request_returns_same_allocation_without_new_randomness(
    store, two_arm_cfg
) -> None:
    seed = 20260927
    first = _enroll(store, two_arm_cfg, seed, "S1", {"site": "A"}, "req-1")
    second = _enroll(store, two_arm_cfg, seed, "S1", {"site": "A"}, "req-2")
    assert second.arm == first.arm
    assert second.replayed is True
    assert second.block_index == first.block_index
    assert second.position_in_block == first.position_in_block
    assert second.request_id == "req-1"  # original request id retained

    events = store.audit_events(two_arm_cfg.study_id)
    types = [e["event_type"] for e in events]
    assert types.count("SUBJECT_ENROLLED") == 1
    assert types.count("ALLOCATION_RETURNED") == 1


@pytest.mark.integration
def test_feature_change_cannot_silently_re_randomise(store, two_arm_cfg) -> None:
    seed = 20260927
    first = _enroll(store, two_arm_cfg, seed, "S1", {"site": "A"}, "req-1")
    with pytest.raises(AllocationError) as exc:
        _enroll(store, two_arm_cfg, seed, "S1", {"site": "B"}, "req-3")
    assert exc.value.category is ErrorCategory.DUPLICATE_CONFLICT
    assert exc.value.http_status == 409
    assert exc.value.details["original_arm"] == first.arm
    assert exc.value.details["original_features"] == {"site": "A"}
    # Original row is untouched and a refusal was audited.
    row = store.lookup_subject(two_arm_cfg.study_id, "S1")
    assert row["arm"] == first.arm
    assert any(
        e["event_type"] == "REALLOCATION_REFUSED"
        for e in store.audit_events(two_arm_cfg.study_id)
    )


@pytest.mark.integration
def test_request_id_reuse_for_different_subject_is_conflict(store, two_arm_cfg) -> None:
    seed = 20260927
    _enroll(store, two_arm_cfg, seed, "S1", {"site": "A"}, "shared-req")
    with pytest.raises(AllocationError) as exc:
        _enroll(store, two_arm_cfg, seed, "S2", {"site": "A"}, "shared-req")
    assert exc.value.category is ErrorCategory.CONFLICT


@pytest.mark.integration
def test_concurrent_enrollments_each_allocate_exactly_once(
    store, two_arm_cfg
) -> None:
    seed = 20260927
    n_threads = 24
    barrier = threading.Barrier(n_threads)
    arms: list[str] = []
    errors: list[str] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        barrier.wait()
        try:
            result = _enroll(
                store, two_arm_cfg, seed, f"C{i}", {"site": "A"}, f"c-req-{i}"
            )
            with lock:
                arms.append(result.arm)
        except AllocationError as exc:  # pragma: no cover - failure path
            with lock:
                errors.append(exc.category.value)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(arms) == n_threads
    subjects = store.subjects(two_arm_cfg.study_id)
    assert len(subjects) == n_threads
    assert len({s["subject_id"] for s in subjects}) == n_threads
    # Block sizes are 2/4: complete blocks are balanced; the open tail
    # may deviate by at most one seat, which diagnostics discloses.
    total_t = sum(s["arm"] == "treatment" for s in subjects)
    assert abs(total_t - n_threads / 2) <= 1


@pytest.mark.integration
def test_concurrent_duplicate_requests_for_one_subject(store, two_arm_cfg) -> None:
    seed = 20260927
    n_threads = 16
    barrier = threading.Barrier(n_threads)
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        result = _enroll(
            store, two_arm_cfg, seed, "DUP", {"site": "A"}, "dup-req"
        )
        with lock:
            outcomes.append(result.arm)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(outcomes) == n_threads
    assert set(outcomes) == {outcomes[0]}  # everyone saw the identical arm
    subjects = store.subjects(two_arm_cfg.study_id)
    assert len([s for s in subjects if s["subject_id"] == "DUP"]) == 1


@pytest.mark.integration
def test_stratum_state_recovers_after_reopen(tmp_path, two_arm_cfg) -> None:
    db = str(tmp_path / "recover.db")
    seed = 20260927
    store1 = Storage(db)
    _enroll(store1, two_arm_cfg, seed, "R1", {"site": "A"}, "r-1")
    _enroll(store1, two_arm_cfg, seed, "R2", {"site": "A"}, "r-2")
    subjects_before = store1.subjects(two_arm_cfg.study_id)
    store1.close()

    store2 = Storage(db)
    # Continue the stream: next two draws must extend, not restart, it.
    r3 = _enroll(store2, two_arm_cfg, seed, "R3", {"site": "A"}, "r-3")
    r4 = _enroll(store2, two_arm_cfg, seed, "R4", {"site": "A"}, "r-4")
    all_subjects = store2.subjects(two_arm_cfg.study_id)
    assert [s["subject_id"] for s in all_subjects] == ["R1", "R2", "R3", "R4"]
    # Sequence indices are continuous.
    assert [s["sequence_index"] for s in all_subjects] == [0, 1, 2, 3]
    # Allocation order agrees with a fresh replay of the same stratum.
    from reference import pbr

    cfg_dict = {
        "arms": list(two_arm_cfg.arms),
        "block_sizes": list(two_arm_cfg.block_sizes),
        "allocation_ratio": list(two_arm_cfg.allocation_ratio),
        "tail_policy": two_arm_cfg.tail_policy.value,
        "master_seed": seed,
        "study_id": two_arm_cfg.study_id,
    }
    oracle = pbr.arm_sequence(cfg_dict, "site=A", 4)
    labels = [two_arm_cfg.arms[i] for i in oracle]
    assert [s["arm"] for s in all_subjects] == labels
    assert r3.arm == labels[2] and r4.arm == labels[3]
    store2.close()
    _ = subjects_before  # fetched before close to prove durability


@pytest.mark.integration
def test_registered_contract_is_frozen(store, two_arm_cfg) -> None:
    seed = 20260927
    store.register_study(two_arm_cfg, seed)
    altered = build_study_config(
        two_arm_cfg.study_id, ["control", "treatment", "third"],
        ["site"], [3, 6], [1, 1, 1],
    )
    with pytest.raises(AllocationError) as exc:
        store.register_study(altered, seed)
    assert exc.value.category is ErrorCategory.CONFLICT
    cfg_back, seed_back = store.get_study(two_arm_cfg.study_id)
    assert cfg_back.arms == ("control", "treatment")
    assert seed_back == seed
