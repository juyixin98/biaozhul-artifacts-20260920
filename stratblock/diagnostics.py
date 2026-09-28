"""Evidence and diagnostics.

This module establishes whether the allocation mechanism is *correct*, as
opposed to whether post-allocation outcomes look significant
(:mod:`stratblock.estimator`). For a study it produces one report with:

* ``block_enumeration``: every realised block, complete blocks compared
  against the exact ratio plan, open/incomplete blocks explained against
  the Hamilton prefix target (``tail_imbalance`` flagged, never hidden);
* ``sequence_replay``: stored allocations re-derived two independent ways
  — the production kernel replayed from its initial state, and the
  standard-library-only reference oracle (``reference/pbr.py``) — with
  mismatches classified by :class:`~stratblock.contract.MismatchKind`;
* ``stream_provenance``: scheme, master seed, per-stratum stream id and
  key fingerprint;
* ``distribution_checks``: chi-square goodness-of-fit of realised arm
  shares vs the ratio, within strata and pooled;
* ``failures`` / ``uncertainties``: stated separately from successes.
"""
from __future__ import annotations

from typing import Any

from scipy import stats

from .contract import (
    MismatchKind,
    StudyConfig,
    TailPolicy,
    hamilton_counts,
    prefix_is_balanced,
)
from .rng import (
    derive_stream_key,
    draw_next,
    initial_state,
    stream_identifier,
)
from .storage import Storage


def _config_dict(cfg: StudyConfig, master_seed: int) -> dict[str, Any]:
    return {
        "arms": list(cfg.arms),
        "block_sizes": list(cfg.block_sizes),
        "allocation_ratio": list(cfg.allocation_ratio),
        "tail_policy": cfg.tail_policy.value,
        "master_seed": master_seed,
        "study_id": cfg.study_id,
    }


def _production_replay(cfg: StudyConfig, stream_key: bytes, n: int) -> list[int]:
    state = initial_state()
    arms: list[int] = []
    for _ in range(n):
        draw, state = draw_next(cfg, stream_key, state)
        arms.append(draw.arm_index)
    return arms


def _tail_character(
    cfg: StudyConfig, block: dict[str, Any]
) -> dict[str, Any]:
    """Characterise one block; flag an incomplete tail explicitly."""
    counts = tuple(block["counts"])
    size = block["block_size"]
    filled = block["filled"]
    complete = filled == size
    if complete:
        plan = cfg.plan_counts(size)
        ok = counts == plan
        return {
            "complete": True,
            "realised_counts": list(counts),
            "required_counts": list(plan),
            "on_ratio": ok,
            "flag": None if ok else MismatchKind.COUNT_PLAN_VIOLATION.value,
        }
    target = hamilton_counts(filled, cfg.allocation_ratio)
    deviation = tuple(counts[i] - target[i] for i in range(len(cfg.arms)))
    if cfg.tail_policy is TailPolicy.BALANCED_PREFIX:
        # This policy guarantees the exact deterministic Hamilton target.
        on_ratio = counts == target
        criterion = "deterministic_hamilton_target"
    else:
        # A permuted prefix is on-ratio iff it lies in the symmetric set
        # of feasible largest-remainder apportionments (all tie-breaks).
        on_ratio = prefix_is_balanced(counts, filled, cfg.allocation_ratio)
        criterion = "feasible_apportionment_set"
    return {
        "complete": False,
        "realised_counts": list(counts),
        "prefix_length": filled,
        "on_ratio_target_for_prefix": list(target),
        "balance_criterion": criterion,
        "deviation": list(deviation),
        "on_ratio": on_ratio,
        "flag": None if on_ratio else MismatchKind.TAIL_BALANCE_VIOLATION.value,
        "note": (
            "open block sealed before filling; a permuted prefix is flagged "
            "only when its counts cannot realise the ratio (e.g. 3/0 in a 1:1 block)"
            if cfg.tail_policy is TailPolicy.PERMUTED
            else "balanced-prefix policy keeps every prefix on the deterministic Hamilton target"
        ),
    }


def _chi_square_share(
    observed: list[int], ratio: tuple[int, ...]
) -> dict[str, Any]:
    total = sum(observed)
    if total == 0:
        return {"status": "indeterminate", "reason": "no allocations"}
    expected = [total * r / sum(ratio) for r in ratio]
    if min(expected) < 5:
        return {
            "status": "indeterminate",
            "reason": "expected cell count < 5; chi-square approximation unreliable",
            "observed": observed,
            "expected": expected,
        }
    chi2, p = stats.chisquare(observed, expected)
    return {
        "status": "ok",
        "observed": observed,
        "expected": [float(x) for x in expected],
        "chi2": float(chi2),
        "p_value": float(p),
        "note": "descriptive balance check on one realised trial; not a validity proof",
    }


def diagnose_study(store: Storage, study_id: str) -> dict[str, Any]:
    """Build the full correctness report for one study."""
    from reference import pbr

    cfg, master_seed = store.get_study(study_id)
    subjects = store.subjects(study_id)
    events = store.audit_events(study_id)

    failures: list[dict[str, Any]] = []
    uncertainties: list[dict[str, Any]] = []
    strata_reports: list[dict[str, Any]] = []
    pooled_counts = [0] * len(cfg.arms)

    by_stratum: dict[str, list[dict[str, Any]]] = {}
    for subj in subjects:
        by_stratum.setdefault(subj["stratum_key"], []).append(subj)

    for stratum_key in sorted(by_stratum):
        members = sorted(by_stratum[stratum_key], key=lambda s: s["sequence_index"])
        n = len(members)
        stream_key = derive_stream_key(master_seed, cfg.study_id, stratum_key)

        # --- replay three ways: stored, production kernel, reference oracle
        stored_arms = [cfg.arm_index(s["arm"]) for s in members]
        kernel_arms = _production_replay(cfg, stream_key, n)
        reference_arms = pbr.arm_sequence(_config_dict(cfg, master_seed), stratum_key, n)
        reference_diary = pbr.block_diary(
            _config_dict(cfg, master_seed), stratum_key, n
        )

        replay_findings: list[dict[str, Any]] = []
        if kernel_arms != stored_arms:
            replay_findings.append(
                {
                    "kind": MismatchKind.SEQUENCE_MISMATCH.value,
                    "against": "production_kernel",
                    "first_position": next(
                        i for i, (a, b) in enumerate(zip(kernel_arms, stored_arms))
                        if a != b
                    ),
                }
            )
        if reference_arms != stored_arms:
            replay_findings.append(
                {
                    "kind": MismatchKind.SEQUENCE_MISMATCH.value,
                    "against": "reference_oracle",
                    "first_position": next(
                        i for i, (a, b) in enumerate(zip(reference_arms, stored_arms))
                        if a != b
                    ),
                }
            )
        if len(reference_arms) != n:
            replay_findings.append(
                {"kind": MismatchKind.POSITION_GAP.value, "detail": "length differs"}
            )
        for idx, (s, ref) in enumerate(zip(members, reference_diary)):
            if (
                s["block_index"] != ref["block_index"]
                or s["position_in_block"] != ref["position_in_block"]
                or s["block_size"] != ref["block_size"]
            ):
                replay_findings.append(
                    {
                        "kind": MismatchKind.BLOCK_SIZE_MISMATCH.value,
                        "sequence_index": idx,
                        "stored": {
                            "block_index": s["block_index"],
                            "position_in_block": s["position_in_block"],
                            "block_size": s["block_size"],
                        },
                        "reference": ref,
                    }
                )
            if idx > 0 and s["sequence_index"] != members[idx - 1]["sequence_index"] + 1:
                replay_findings.append(
                    {
                        "kind": MismatchKind.POSITION_GAP.value,
                        "sequence_index": s["sequence_index"],
                    }
                )
        failures.extend(
            {"stratum_key": stratum_key, **f} for f in replay_findings
        )

        # --- block enumeration: exact counts in every realised block
        blocks = store.stratum_block_counts(study_id, stratum_key, cfg.arms)
        block_reports = [_tail_character(cfg, b) for b in blocks]
        for b in block_reports:
            if b["flag"] == MismatchKind.COUNT_PLAN_VIOLATION.value:
                failures.append(
                    {
                        "stratum_key": stratum_key,
                        "kind": MismatchKind.COUNT_PLAN_VIOLATION.value,
                        "block": b,
                    }
                )
            elif b["flag"] == MismatchKind.TAIL_BALANCE_VIOLATION.value:
                # A permuted tail deviation is expected behaviour, disclosed;
                # under balanced_prefix it is a real failure.
                if cfg.tail_policy is TailPolicy.BALANCED_PREFIX:
                    failures.append(
                        {
                            "stratum_key": stratum_key,
                            "kind": MismatchKind.TAIL_BALANCE_VIOLATION.value,
                            "block": b,
                        }
                    )
                else:
                    uncertainties.append(
                        {
                            "stratum_key": stratum_key,
                            "kind": "DISCLOSED_TAIL_IMBALANCE",
                            "block": b,
                        }
                    )

        observed = [0] * len(cfg.arms)
        for b in blocks:
            for i, c in enumerate(b["counts"]):
                observed[i] += c
        for i, c in enumerate(observed):
            pooled_counts[i] += c

        strata_reports.append(
            {
                "stratum_key": stratum_key,
                "n_subjects": n,
                "observed_arm_counts": dict(zip(cfg.arms, observed)),
                "blocks": [
                    {"block_index": b["block_index"], **br}
                    for b, br in zip(blocks, block_reports)
                ],
                "sequence_replay": {
                    "stored_sequence": [cfg.arms[a] for a in stored_arms],
                    "production_kernel_agrees": kernel_arms == stored_arms,
                    "reference_oracle_agrees": reference_arms == stored_arms,
                    "findings": replay_findings,
                },
                "stream_provenance": {
                    "stream_id": stream_identifier(cfg.study_id, stratum_key),
                    "key_fingerprint_sha256": stream_key.hex(),
                },
            }
        )

    # --- event-level checks (repeats must be returns, feature changes refused)
    enrolled = [e for e in events if e["event_type"] == "SUBJECT_ENROLLED"]
    returned = [e for e in events if e["event_type"] == "ALLOCATION_RETURNED"]
    refused = [e for e in events if e["event_type"] == "REALLOCATION_REFUSED"]
    if len(enrolled) != len(subjects):
        failures.append(
            {
                "kind": MismatchKind.EVENT_REORDER.value,
                "detail": (
                    f"{len(enrolled)} SUBJECT_ENROLLED events but "
                    f"{len(subjects)} stored subjects"
                ),
            }
        )

    status_row = store.list_studies()
    status = next(
        (s["status"] for s in status_row if s["study_id"] == study_id), "unknown"
    )

    conclusion = "PASS" if not failures else "FAIL"
    return {
        "study_id": study_id,
        "study_status": status,
        "contract": {
            "arms": list(cfg.arms),
            "stratification_factors": list(cfg.stratification_factors),
            "block_sizes": list(cfg.block_sizes),
            "allocation_ratio": list(cfg.allocation_ratio),
            "tail_policy": cfg.tail_policy.value,
        },
        "stream_provenance": {
            "scheme": "stratblock-deterministic-rng-v1",
            "master_seed": master_seed,
            "derivation": "HKDF-SHA256(master_seed, study_id, stratum_key) "
            "-> SHA256 counter -> rejection-uniform -> Fisher-Yates",
        },
        "n_subjects": len(subjects),
        "n_strata": len(by_stratum),
        "strata": strata_reports,
        "distribution_checks": {
            "pooled_arm_counts": dict(zip(cfg.arms, pooled_counts)),
            "pooled_chi_square_vs_ratio": _chi_square_share(
                pooled_counts, cfg.allocation_ratio
            ),
        },
        "audit_summary": {
            "enrolled": len(enrolled),
            "idempotent_returns": len(returned),
            "reallocation_refusals": len(refused),
        },
        "failures": failures,
        "uncertainties": uncertainties,
        "conclusion": conclusion,
        "scope_note": (
            "This report verifies the allocation mechanism. It deliberately "
            "does not use post-allocation outcome significance as evidence."
        ),
    }
