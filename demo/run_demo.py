"""End-to-end reproducible demonstration.

Runs entirely against local synthetic data and a local SQLite file:

1. registers two studies (permuted blocks and balanced-prefix);
2. enrolls fixed subjects with fixed request ids under a fixed seed;
3. exercises repeat requests (same arm returned) and a feature-changing
   repeat (refused, audited);
4. seals enrollment and shows the tail is closed to new subjects while
   repeat requests still resolve;
5. enters synthetic outcomes and builds the effect report;
6. writes diagnostics / replay / distribution artifacts to
   ``evidence/output`` for inspection.

Run::

    python3 -m demo.run_demo
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from scipy import stats

from reference import pbr
from stratblock.contract import (
    AllocationError,
    TailPolicy,
    build_study_config,
)
from stratblock.diagnostics import diagnose_study
from stratblock.estimator import build_effect_report
from stratblock.replay import ScriptRequest, replay_from_audit, run_script
from stratblock.rng import draw_next
from stratblock.storage import Storage

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "evidence" / "output"
DB_PATH = ROOT / "demo" / "demo.db"
MASTER_SEED = 20260927

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("demo")


def _write(name: str, payload: object) -> Path:
    path = OUT / name
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return path


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(DB_PATH) + suffix)
        if path.exists():
            path.unlink()

    store = Storage(str(DB_PATH))

    # ------------------------------------------------------------- study A
    study_a = build_study_config(
        study_id="demo-permuted",
        arms=["control", "treatment"],
        stratification_factors=["site"],
        block_sizes=[2, 4],
        allocation_ratio=[1, 1],
        tail_policy=TailPolicy.PERMUTED,
    )
    requests_a = [
        ScriptRequest(f"P{i:03d}", {"site": "A"}, f"req-a-{i:03d}")
        for i in range(8)
    ] + [
        ScriptRequest(f"P{i:03d}", {"site": "B"}, f"req-a-{i:03d}")
        for i in range(8, 13)
    ]
    result_a = run_script(study_a, MASTER_SEED, requests_a, store)
    log.info("study A allocated %d subjects", len(result_a.allocations))

    # Repeat request: same arm, no new randomness.
    repeat = store.enroll(
        study_a, MASTER_SEED, "P000", {"site": "A"}, "req-repeat-000",
        "enroller", draw_next,
    )
    log.info("repeat P000 -> arm=%s replayed=%s", repeat.arm, repeat.replayed)

    # Feature-changing repeat: refused and audited.
    try:
        store.enroll(
            study_a, MASTER_SEED, "P000", {"site": "B"}, "req-feature-change",
            "enroller", draw_next,
        )
    except AllocationError as exc:
        log.info(
            "feature change refused: category=%s original_arm=%s",
            exc.category.value, exc.details["original_arm"],
        )

    # Synthetic outcomes: treatment shifts the mean by 2 (synthetic truth).
    # Jitter is a deterministic function of the subject number so reruns
    # are byte-identical (never Python's salted built-in hash()).
    for subj in store.subjects("demo-permuted"):
        idx = int(subj["subject_id"].removeprefix("P"))
        jitter = (idx % 7) / 100.0 - 0.03
        y = (2.0 if subj["arm"] == "treatment" else 0.0) + jitter
        store.record_outcome("demo-permuted", subj["subject_id"], y)

    # ------------------------------------------------------------- study B
    study_b = build_study_config(
        study_id="demo-balanced",
        arms=["C", "T1", "T2"],
        stratification_factors=["age", "sex"],
        block_sizes=[3, 6],
        allocation_ratio=[1, 1, 1],
        tail_policy=TailPolicy.BALANCED_PREFIX,
    )
    requests_b = [
        ScriptRequest(f"B{i:03d}", {"age": "old", "sex": "F"}, f"req-b-{i:03d}")
        for i in range(5)
    ]
    run_script(study_b, 7, requests_b, store)

    # ------------------------------------------------- study C: open tail
    # Block size 4, seal after 2 enrollments. A 1:1 prefix of length 2 is
    # only balanced as (1,1); a realised (2,0)/(0,2) cannot realise the
    # ratio and must be disclosed as a tail imbalance. We deterministically
    # choose (via the independent oracle, before any enrollment) the first
    # fixed stratum key that realises such an infeasible prefix, so the
    # demo always exhibits the disclosed-tail path reproducibly.
    study_c = build_study_config(
        study_id="demo-tail",
        arms=["control", "treatment"],
        stratification_factors=["site"],
        block_sizes=[4],
        allocation_ratio=[1, 1],
        tail_policy=TailPolicy.PERMUTED,
    )
    tail_cfg_dict = {
        "arms": list(study_c.arms),
        "block_sizes": list(study_c.block_sizes),
        "allocation_ratio": list(study_c.allocation_ratio),
        "tail_policy": study_c.tail_policy.value,
        "master_seed": MASTER_SEED,
        "study_id": study_c.study_id,
    }
    tail_key = next(
        f"site=T{i}"
        for i in range(50)
        if (
            lambda seq: (seq.count(0), seq.count(1)) in {(2, 0), (0, 2)}
        )(pbr.arm_sequence(tail_cfg_dict, f"site=T{i}", 2))
    )
    tail_level = tail_key.removeprefix("site=")
    requests_c = [
        ScriptRequest(f"T{i:03d}", {"site": tail_level}, f"req-c-{i:03d}")
        for i in range(2)
    ]
    run_script(study_c, MASTER_SEED, requests_c, store)
    store.seal_study("demo-tail")

    # --------------------------------------------------- tail closure on A
    store.seal_study("demo-permuted")
    try:
        store.enroll(
            study_a, MASTER_SEED, "AFTER-SEAL", {"site": "A"}, "req-after-seal",
            "enroller", draw_next,
        )
    except AllocationError as exc:
        seal_block = {
            "category": exc.category.value,
            "http_status": exc.http_status,
            "message": exc.message,
        }
        log.info("post-seal enrollment: %s", exc.category.value)
    else:  # pragma: no cover - would be a real defect
        seal_block = {"category": "UNEXPECTED_SUCCESS"}

    post_seal_repeat = store.enroll(
        study_a, MASTER_SEED, "P000", {"site": "A"}, "req-repeat-after-seal",
        "enroller", draw_next,
    )
    log.info(
        "post-seal repeat P000 -> arm=%s replayed=%s",
        post_seal_repeat.arm, post_seal_repeat.replayed,
    )

    # ------------------------------------------------------------ evidence
    diag_a = diagnose_study(store, "demo-permuted")
    diag_b = diagnose_study(store, "demo-balanced")
    diag_c = diagnose_study(store, "demo-tail")
    replay_a = replay_from_audit(store, "demo-permuted")

    grouped = store.outcomes("demo-permuted")
    effect = build_effect_report(
        "treatment", [v for _, v in grouped["treatment"]],
        "control", [v for _, v in grouped["control"]],
        permutation_seed=MASTER_SEED & 0x7FFFFFFF,
    )

    # Distribution evidence across FIXED seeds (independent oracle only).
    dist_cfg = {
        "arms": ["control", "treatment"],
        "block_sizes": [4],
        "allocation_ratio": [1, 1],
        "tail_policy": "permuted",
        "master_seed": MASTER_SEED,
        "study_id": "dist-check",
    }
    pooled = [0, 0]
    seeds = (1, 2, 3, 7, 42, 99, 1234, MASTER_SEED)
    for seed in seeds:
        for level in range(25):
            seq = pbr.arm_sequence({**dist_cfg, "master_seed": seed}, f"s={level}", 8)
            for arm in seq:
                pooled[arm] += 1
    chi2, p_value = stats.chisquare(pooled, [sum(pooled) / 2, sum(pooled) / 2])
    distribution = {
        "fixed_seeds": list(seeds),
        "strata_per_seed": 25,
        "draws_per_stratum": 8,
        "pooled_arm_counts": {"control": pooled[0], "treatment": pooled[1]},
        "chi_square": float(chi2),
        "p_value": float(p_value),
        "note": "fixed-seed marginal fairness check; not a validity proof",
    }

    artifacts = {
        "diagnostics_demo_permuted.json": diag_a,
        "diagnostics_demo_balanced.json": diag_b,
        "diagnostics_demo_tail.json": diag_c,
        "replay_demo_permuted.json": replay_a,
        "effect_demo_permuted.json": effect,
        "distribution_fixed_seeds.json": distribution,
        "seal_behavior.json": seal_block,
    }
    for name, payload in artifacts.items():
        path = _write(name, payload)
        log.info("wrote %s", path.relative_to(ROOT))

    log.info(
        "summary: diag A=%s replay A=%s effect proves_allocation_correct=%s",
        diag_a["conclusion"], replay_a["conclusion"],
        effect["proves_allocation_correct"],
    )
    store.close()


if __name__ == "__main__":
    main()
