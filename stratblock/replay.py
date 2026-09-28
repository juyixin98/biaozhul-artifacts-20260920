"""Reproducible experiments.

Three facilities:

* :func:`run_script` — execute a fixed enrollment script (study + ordered
  requests) against a fresh in-memory or file storage and return a
  transcript; used by tests and demos so a run is byte-reproducible.
* :func:`replay_from_audit` — rebuild the expected arm for every
  ``SUBJECT_ENROLLED`` audit event, in event order, using the production
  kernel, and diff against the recorded payload. Any divergence is a
  :data:`REPLAY_MISMATCH` with position details.
* :func:`tail_closure_experiment` — enroll, seal mid-block, prove the tail
  is closed (further enrollment is refused) and that the sealed tail
  report is disclosed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .contract import (
    AllocationError,
    ErrorCategory,
    StudyConfig,
    hamilton_counts,
)
from .rng import derive_stream_key, draw_next, initial_state, stream_identifier
from .storage import EnrollmentResult, Storage


@dataclass(frozen=True)
class ScriptRequest:
    subject_id: str
    features: dict[str, str]
    request_id: str


@dataclass(frozen=True)
class ScriptOutcome:
    subject_id: str
    y: float


@dataclass(frozen=True)
class ExperimentResult:
    study_id: str
    allocations: tuple[EnrollmentResult, ...]
    storage: Storage
    repeats: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    refused: tuple[dict[str, Any], ...] = field(default_factory=tuple)


def run_script(
    cfg: StudyConfig,
    master_seed: int,
    requests: list[ScriptRequest],
    storage: Storage,
    actor: str = "enroller",
) -> ExperimentResult:
    """Register ``cfg`` (if needed) and execute ``requests`` in order.

    Repeat submissions (same subject appearing twice) are captured rather
    than raised here, so tests can assert the exact failure category.
    """
    try:
        storage.register_study(cfg, master_seed)
    except AllocationError as exc:
        if exc.category is not ErrorCategory.CONFLICT:
            raise

    allocations: list[EnrollmentResult] = []
    repeats: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    for req in requests:
        try:
            result = storage.enroll(
                cfg,
                master_seed,
                req.subject_id,
                req.features,
                req.request_id,
                actor,
                draw_next,
            )
        except AllocationError as exc:
            entry = {
                "subject_id": req.subject_id,
                "request_id": req.request_id,
                "category": exc.category.value,
                "message": exc.message,
                "details": exc.details,
            }
            if exc.category is ErrorCategory.DUPLICATE_CONFLICT:
                refused.append(entry)
            else:
                raise
        else:
            if result.replayed:
                repeats.append(
                    {
                        "subject_id": result.subject_id,
                        "arm": result.arm,
                        "original_request_id": result.request_id,
                    }
                )
            else:
                allocations.append(result)
    return ExperimentResult(
        study_id=cfg.study_id,
        allocations=tuple(allocations),
        storage=storage,
        repeats=tuple(repeats),
        refused=tuple(refused),
    )


def replay_from_audit(store: Storage, study_id: str) -> dict[str, Any]:
    """Re-derive every allocation from audit events and compare."""
    cfg, master_seed = store.get_study(study_id)
    events = store.audit_events(study_id)
    enrolled = [e for e in events if e["event_type"] == "SUBJECT_ENROLLED"]

    states: dict[str, Any] = {}
    mismatches: list[dict[str, Any]] = []
    checked = 0
    for event in enrolled:
        payload = event["payload"]
        key = payload["stratum_key"]
        stream_key = derive_stream_key(master_seed, study_id, key)
        state = states.get(key, initial_state())
        draw, new_state = draw_next(cfg, stream_key, state)
        states[key] = new_state

        expected_arm = cfg.arms[draw.arm_index]
        recorded_arm = payload["arm"]
        if expected_arm != recorded_arm:
            mismatches.append(
                {
                    "kind": "SEQUENCE_MISMATCH",
                    "event_id": event["id"],
                    "subject_id": event["subject_id"],
                    "request_id": event["request_id"],
                    "stratum_key": key,
                    "expected_arm": expected_arm,
                    "recorded_arm": recorded_arm,
                    "sequence_index": payload["sequence_index"],
                }
            )
        if payload["sequence_index"] != draw.sequence_index:
            mismatches.append(
                {
                    "kind": "POSITION_GAP",
                    "event_id": event["id"],
                    "subject_id": event["subject_id"],
                    "expected_sequence_index": draw.sequence_index,
                    "recorded_sequence_index": payload["sequence_index"],
                }
            )
        checked += 1

    return {
        "study_id": study_id,
        "events_checked": checked,
        "stream_scheme": "stratblock-deterministic-rng-v1",
        "master_seed": master_seed,
        "mismatches": mismatches,
        "conclusion": "PASS" if not mismatches else "FAIL",
        "uncertainties": [] if checked else [
            {
                "kind": "NO_EVENTS",
                "message": "study has no SUBJECT_ENROLLED events; nothing to replay",
            }
        ],
    }


def tail_closure_experiment(
    cfg: StudyConfig,
    master_seed: int,
    requests: list[ScriptRequest],
    storage: Storage,
) -> dict[str, Any]:
    """Enroll a script, seal the study, and document the closed tails."""
    experiment = run_script(cfg, master_seed, requests, storage)
    storage.seal_study(cfg.study_id)

    blocked: list[dict[str, Any]] = []
    for req in requests[: min(3, len(requests))]:
        try:
            storage.enroll(
                cfg, master_seed, f"post-seal-{req.subject_id}",
                req.features, f"post-seal-{req.request_id}",
                "enroller", draw_next,
            )
        except AllocationError as exc:
            blocked.append(
                {
                    "subject_id": f"post-seal-{req.subject_id}",
                    "category": exc.category.value,
                    "http_status": exc.http_status,
                    "message": exc.message,
                }
            )

    # Repeat requests for already-enrolled subjects must still resolve
    # (returning the original arm) even after sealing.
    repeat_ok: list[dict[str, Any]] = []
    for req in requests[: min(3, len(requests))]:
        result = storage.enroll(
            cfg, master_seed, req.subject_id, req.features,
            f"post-seal-repeat-{req.request_id}", "enroller", draw_next,
        )
        repeat_ok.append(
            {"subject_id": result.subject_id, "arm": result.arm, "replayed": result.replayed}
        )

    tails: list[dict[str, Any]] = []
    for key, state in storage.list_strata(cfg.study_id):
        blocks = storage.stratum_block_counts(cfg.study_id, key, cfg.arms)
        if not blocks:
            continue
        last = blocks[-1]
        if last["filled"] < last["block_size"]:
            target = hamilton_counts(last["filled"], cfg.allocation_ratio)
            tails.append(
                {
                    "stratum_key": key,
                    "block_index": last["block_index"],
                    "block_size": last["block_size"],
                    "filled": last["filled"],
                    "realised_counts": dict(zip(cfg.arms, last["counts"])),
                    "on_ratio_target_for_prefix": dict(zip(cfg.arms, target)),
                    "sealed": state.sealed,
                    "stream_id": stream_identifier(cfg.study_id, key),
                }
            )

    return {
        "study_id": cfg.study_id,
        "tail_policy": cfg.tail_policy.value,
        "post_seal_enrollments_blocked": blocked,
        "post_seal_repeat_requests": repeat_ok,
        "open_blocks_at_seal": tails,
        "n_allocated": len(experiment.allocations),
    }
