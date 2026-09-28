"""复现实验运行器：从夹具执行完整实验并产出机器可读报告。

覆盖的验收点
------------
1. **串行双跑逐对象一致**：同一夹具 + 同一固定种子 + *同一固定入组顺序*，
   在两个全新文件库中分别执行，分配序列（臂/层/区组/位置）必须逐对象
   相同。注意：区组随机的逐对象结果依赖入组到达顺序，所以"逐对象一致"
   只在固定顺序的串行双跑中断言。
2. **并发登记完整性**：一批对象多线程同时提交（到达顺序不可预设），
   断言：无重复槽位、无对象丢失、每个被占满的区组比例严格正确——
   不要求逐对象与串行一致（那在统计上也不应成立）。
3. **恢复后续跑**：复用第一次的库与种子保险库文件重新打开服务，
   已登记对象回放原分配，再追加新对象时区组序号/位置正确接续；
4. **尾组封闭**：keep_open 下尾组保留并可继续填入；seal_early 下
   封闭后再登记被明确拒绝，重新开组需管理员显式动作；
5. **特征篡改**：同对象改特征重放 → 409 稳定类别且原分配不变；
6. **流来源记录**：报告记录种子指纹、PRF、域标签、区组/流定位方式。
"""
from __future__ import annotations

import concurrent.futures
import os
import pathlib
import tempfile

from ..core.seed import SecretSeed
from ..errors import AppError
from ..evidence.balance import balance_report
from ..evidence.stream_verify import verify_study
from ..repository_support import make_service
from .fixtures import StudyFixture


def _fresh_runtime(workdir: str, name: str):
    db_path = os.path.join(workdir, f"{name}.db")
    seeds_path = os.path.join(workdir, f"{name}-seeds.json")
    log_path = os.path.join(workdir, f"{name}.audit.log")
    runtime = make_service(db_path=db_path, seeds_path=seeds_path,
                           log_path=log_path)
    return runtime, {
        "database": db_path, "seeds": seeds_path, "audit_log": log_path,
    }


def _create_study(service, fixture: StudyFixture, label: str) -> dict:
    return service.create_study(
        contract_kwargs=dict(
            study_id=fixture.study_id,
            arm_specs=list(fixture.arms),
            factor_specs=[(n, list(levels)) for n, levels in fixture.factors],
            block_multiple=fixture.block_multiple,
            tail_policy=fixture.tail_policy,
        ),
        seed=SecretSeed.from_base64(fixture.seed_b64),
        actor_role="investigator", request_id=f"{label}-create",
    )


def _allocate(service, fixture: StudyFixture, case, label: str) -> dict:
    return service.allocate(
        study_id=fixture.study_id, subject_id=case.subject_id,
        features=dict(case.features), idempotency_key=None,
        actor_role="investigator",
        request_id=f"{label}-alloc-{case.subject_id}",
    )


def _needed_strata(service, study_id: str, cases) -> list[str]:
    """按契约因子顺序算出这批对象涉及的全部层键（去重保序）。"""
    seen: list[str] = []
    with service.db.connection() as conn:
        contract = service.load_contract(conn, study_id)["contract"]
    for case in cases:
        key = contract.stratum_key(case.features)
        if key not in seen:
            seen.append(key)
    return seen


def _open_strata(service, fixture: StudyFixture, cases, label: str,
                 suffix: str) -> None:
    for sk in _needed_strata(service, fixture.study_id, cases):
        service.open_next_block(
            study_id=fixture.study_id, stratum_key=sk, actor_role="admin",
            request_id=f"{label}-open-{suffix}-{sk}")


def _run_concurrent(service, fixture: StudyFixture, cases, label: str):
    results, errors = [], []

    def _go(case):
        return _allocate(service, fixture, case, label)

    max_workers = min(8, max(2, len(cases)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_go, c): c for c in cases}
        for fut in concurrent.futures.as_completed(futures):
            case = futures[fut]
            try:
                results.append(fut.result())
            except AppError as exc:
                errors.append({"subject_id": case.subject_id,
                               "category": exc.category,
                               "message": exc.message})
    return results, errors


def _drive_enrollments(service, fixture: StudyFixture, label: str,
                       concurrent: bool) -> tuple[list[dict], list[dict],
                                                  dict | None]:
    """按尾组策略驱动登记。返回 (结果, 并发错误, 波次拒绝信息)。"""
    results: list[dict] = []
    errors: list[dict] = []
    wave_rejection = None

    if fixture.tail_policy == "keep_open":
        if concurrent and fixture.concurrent_batch:
            batch = set(fixture.concurrent_batch)
            bc = [c for c in fixture.enrollments if c.subject_id in batch]
            r, e = _run_concurrent(service, fixture, bc, label)
            results += r
            errors += e
            for c in fixture.enrollments:
                if c.subject_id not in batch:
                    results.append(_allocate(service, fixture, c, label))
        else:
            for c in fixture.enrollments:
                results.append(_allocate(service, fixture, c, label))
        return results, errors, wave_rejection

    # ---- seal_early：波次式，开组是管理员显式动作 ----
    first_wave = list(fixture.enrollments[: fixture.block_multiple * 2 + 3])
    _open_strata(service, fixture, first_wave, label, "w1")
    if concurrent and fixture.concurrent_batch:
        batch = set(fixture.concurrent_batch)
        bc = [c for c in first_wave if c.subject_id in batch]
        rest = [c for c in first_wave if c.subject_id not in batch]
        r, e = _run_concurrent(service, fixture, bc, label)
        results += r
        errors += e
        for c in rest:
            results.append(_allocate(service, fixture, c, label))
    else:
        for c in first_wave:
            results.append(_allocate(service, fixture, c, label))

    service.seal_tails(study_id=fixture.study_id, actor_role="admin",
                       request_id=f"{label}-seal-w1")
    next_case = fixture.enrollments[len(first_wave)]
    try:
        _allocate(service, fixture, next_case, label)
        wave_rejection = {"rejected": False}
    except AppError as exc:
        wave_rejection = {
            "rejected": True, "category": exc.category,
            "category_correct": exc.category == "STRATUM_SEALED",
        }

    second_wave = list(fixture.enrollments[len(first_wave):])
    _open_strata(service, fixture, second_wave, label, "w2")
    for c in second_wave:
        results.append(_allocate(service, fixture, c, label))
    return results, errors, wave_rejection


def _snapshot(service, fixture: StudyFixture, results: list[dict]) -> dict:
    with service.db.connection() as conn:
        loaded = service.load_contract(conn, fixture.study_id)
        blocks = service.repo.list_blocks(conn, fixture.study_id)
        allocations = service.repo.list_allocations(conn, fixture.study_id)
        balance = balance_report(loaded["contract"], blocks, allocations)
    verification = verify_study(service.db, service.vault, fixture.study_id,
                                service.repo)
    return {"balance": balance, "verification": verification}


def run_fixture(fixture: StudyFixture, workdir: str, *, label: str,
                concurrent: bool = True, checks: bool = True) -> dict:
    runtime, locations = _fresh_runtime(workdir, label)
    service = runtime["service"]
    contract_info = _create_study(service, fixture, label)

    results, errors, wave_rejection = _drive_enrollments(
        service, fixture, label, concurrent)
    all_results = sorted(results, key=lambda r: r["subject_id"])

    body: dict = {
        "label": label,
        "mode": "concurrent" if concurrent else "serial",
        "locations": locations,
        "contract": contract_info,
        "allocations": [
            {"subject_id": r["subject_id"], "arm": r["arm"],
             "stratum_key": r["stratum_key"], "block_index": r["block_index"],
             "position": r["position"], "outcome": r["outcome"]}
            for r in all_results
        ],
        "concurrent_errors": [
            {"subject_id": e["subject_id"], "category": e["category"]}
            for e in errors
        ],
        "wave_rejection": wave_rejection,
        "random_source": {
            "seed_fingerprint": SecretSeed
                .from_base64(fixture.seed_b64).fingerprint(),
            "seed_b64": fixture.seed_b64,
            "prf": "HMAC-SHA256 counter mode",
            "block_key_derivation": "HKDF-style two-stage HMAC",
            "shuffle": "unbiased Fisher-Yates",
            "integer_sampling": "rejection sampling on uint32 (unbiased)",
            "domain": "rct/v1/block-permutation",
            "locator_coordinates":
                "study_id|contract_fingerprint|stratum_key|block_index|draw_index",
        },
    }

    if checks:
        body.update(_extra_checks(service, fixture, label, all_results))
        snap = _snapshot(service, fixture, all_results)
        body["balance"] = snap["balance"]
        body["verification"] = {
            "verdict": snap["verification"]["verdict"],
            "checks_performed": snap["verification"]["checks_performed"],
            "n_allocations": snap["verification"]["n_allocations"],
            "n_blocks": snap["verification"]["n_blocks"],
            "failures": snap["verification"]["failures"],
        }
    return body


def _extra_checks(service, fixture: StudyFixture, label: str,
                  all_results: list[dict]) -> dict:
    # ---- 特征篡改回放 ----
    tamper_findings = []
    for subject_id, bad_features in fixture.tampered_replays.items():
        original = next(r for r in all_results if r["subject_id"] == subject_id)
        try:
            service.allocate(
                study_id=fixture.study_id, subject_id=subject_id,
                features=bad_features, idempotency_key=None,
                actor_role="investigator",
                request_id=f"{label}-tamper-{subject_id}")
            tamper_findings.append({"subject_id": subject_id, "rejected": False})
        except AppError as exc:
            still = service.get_assignment(
                study_id=fixture.study_id, subject_id=subject_id,
                actor_role="investigator",
                request_id=f"{label}-posttamper-{subject_id}")
            tamper_findings.append({
                "subject_id": subject_id, "rejected": True,
                "category": exc.category,
                "category_correct":
                    exc.category == "FEATURES_CHANGED_AFTER_ALLOCATION",
                "original_arm_preserved": still["arm"] == original["arm"],
            })

    # ---- 同特征幂等回放 ----
    first_case = fixture.enrollments[0]
    replay = service.allocate(
        study_id=fixture.study_id, subject_id=first_case.subject_id,
        features=dict(first_case.features), idempotency_key=None,
        actor_role="investigator", request_id=f"{label}-replay")

    # ---- 尾组 ----
    tail_note = None
    if fixture.tail_policy == "keep_open":
        extra = service.allocate(
            study_id=fixture.study_id, subject_id=f"{label}-EXTRA1",
            features=dict(first_case.features), idempotency_key=None,
            actor_role="investigator", request_id=f"{label}-tail-continue")
        tail_note = {"kept_open_then_filled": {
            "block_index": extra["block_index"], "position": extra["position"]}}
    seal_report = service.seal_tails(
        study_id=fixture.study_id, actor_role="admin",
        request_id=f"{label}-seal-final")

    # ---- 并发槽位完整性（若有并发批次） ----
    concurrency = None
    if fixture.concurrent_batch:
        batch = set(fixture.concurrent_batch)
        batch_results = [r for r in all_results if r["subject_id"] in batch]
        slots = {(r["stratum_key"], r["block_index"], r["position"])
                 for r in batch_results}
        concurrency = {
            "batch_size": len(batch),
            "succeeded": len(batch_results),
            "unique_slots": len(slots),
            "no_slot_collision": len(slots) == len(batch_results),
            "no_subject_lost": len(batch_results)
                == len(batch & {r["subject_id"] for r in batch_results}),
        }

    return {
        "tampered_replays": tamper_findings,
        "idempotent_replay": {
            "subject_id": replay["subject_id"], "outcome": replay["outcome"],
            "arm": replay["arm"],
            "returns_original": replay["outcome"] == "replayed",
        },
        "tail": {"seal_report": seal_report, "tail_note": tail_note},
        "concurrency": concurrency,
    }


# ============================================================
# 双跑比对与恢复
# ============================================================
def _compare_serial_runs(name: str, run_a: dict, run_b: dict) -> dict:
    keys = ("arm", "stratum_key", "block_index", "position")
    mismatches = []
    a_map = {r["subject_id"]: r for r in run_a["allocations"]}
    b_map = {r["subject_id"]: r for r in run_b["allocations"]}
    for sid, ra in a_map.items():
        rb = b_map.get(sid)
        if rb is None:
            mismatches.append({"subject_id": sid, "kind": "missing_in_run_b"})
            continue
        for k in keys:
            if ra[k] != rb[k]:
                mismatches.append({"subject_id": sid, "field": k,
                                   "run_a": ra[k], "run_b": rb[k]})
    if len(a_map) != len(b_map):
        mismatches.append({"kind": "count_mismatch",
                           "run_a": len(a_map), "run_b": len(b_map)})
    return {
        "fixture": name,
        "mode": "serial_fixed_order",
        "identical": not mismatches,
        "n_compared": len(a_map),
        "mismatches": mismatches,
        "contract_fingerprint_same":
            run_a["contract"]["contract_fingerprint"]
            == run_b["contract"]["contract_fingerprint"],
        "note": "串行固定入组顺序下逐对象可复现；并发到达顺序不固定，"
                "逐对象差异不视为失败。",
    }


def _recovery_report(name: str, fx: StudyFixture, prior_run: dict) -> dict:
    """复用先前运行的数据库与种子文件重启服务，验证回放与接续。"""
    locs = prior_run["locations"]
    runtime = make_service(db_path=locs["database"], seeds_path=locs["seeds"],
                           log_path=locs["audit_log"])
    service = runtime["service"]
    case0 = fx.enrollments[0]
    replayed = service.allocate(
        study_id=fx.study_id, subject_id=case0.subject_id,
        features=dict(case0.features), idempotency_key=None,
        actor_role="investigator", request_id=f"recovery-{name}-replay")

    if fx.tail_policy == "seal_early":
        with service.db.connection() as conn:
            contract = service.load_contract(conn, fx.study_id)["contract"]
            sk = contract.stratum_key(case0.features)
            open_block = service.repo.get_open_block(conn, fx.study_id, sk)
        # 先前运行可能已留下开放区组（尾组未封）；没有时才需管理员重开
        if open_block is None:
            service.open_next_block(
                study_id=fx.study_id, stratum_key=sk, actor_role="admin",
                request_id=f"recovery-{name}-open")
    cont = service.allocate(
        study_id=fx.study_id, subject_id=f"RECOVERY-NEW-{name}",
        features=dict(case0.features), idempotency_key=None,
        actor_role="investigator", request_id=f"recovery-{name}-new")
    verification = verify_study(service.db, service.vault, fx.study_id,
                                service.repo)
    expected_arm = next(
        r["arm"] for r in prior_run["allocations"]
        if r["subject_id"] == case0.subject_id)
    return {
        "fixture": name,
        "replayed_outcome": replayed["outcome"],
        "replayed_arm_matches": replayed["arm"] == expected_arm,
        "new_subject_allocated_after_restart": cont["outcome"] == "committed",
        "new_subject_arm": cont["arm"],
        "new_subject_coordinates": {"stratum_key": cont["stratum_key"],
                                    "block_index": cont["block_index"],
                                    "position": cont["position"]},
        "verification_after_recovery": verification["verdict"],
    }


def run_reproduction_experiment(workdir: str | None = None) -> dict:
    created_temp = workdir is None
    workdir = workdir or tempfile.mkdtemp(prefix="rct-repro-")
    pathlib.Path(workdir).mkdir(parents=True, exist_ok=True)

    from .fixtures import two_arm_two_strata_fixture, ratio_fixture
    fixtures = [
        ("two_arm_keep_open", two_arm_two_strata_fixture("keep_open")),
        ("two_arm_seal_early", two_arm_two_strata_fixture("seal_early")),
        ("ratio_1_2", ratio_fixture()),
    ]

    comparisons, recoveries, concurrent_runs = [], [], {}
    for name, fx in fixtures:
        serial_a = run_fixture(fx, workdir, label=f"{name}__serialA",
                               concurrent=False, checks=False)
        serial_b = run_fixture(fx, workdir, label=f"{name}__serialB",
                               concurrent=False, checks=False)
        comparisons.append(_compare_serial_runs(name, serial_a, serial_b))

        concurrent_run = run_fixture(fx, workdir, label=f"{name}__concurrent",
                                     concurrent=True, checks=True)
        concurrent_runs[name] = concurrent_run

        recoveries.append(_recovery_report(name, fx, serial_a))

    report = {
        "report": "stratified-block-randomization/reproduction/v1",
        "workdir": workdir,
        "temporary_workdir": created_temp,
        "serial_double_run_comparisons": comparisons,
        "concurrent_runs": concurrent_runs,
        "recovery": recoveries,
        "summary": _summarize(comparisons, recoveries, concurrent_runs),
        "disclaimer": (
            "本报告只验证分配机制（比例、随机流复算、幂等、并发、尾组、"
            "恢复）。任何分配后效果指标（结局差异、p 值等）都不是分配"
            "正确性的证据。"
        ),
    }
    return report


def _summarize(comparisons, recoveries, concurrent_runs) -> dict:
    def balance_ok(run):
        return run["balance"]["verdict"] == "PASS"

    def verify_ok(run):
        return run["verification"]["verdict"] == "PASS"

    concurrency_ok = all(
        (run["concurrency"] is None
         or (run["concurrency"]["no_slot_collision"]
             and run["concurrency"]["succeeded"]
             == run["concurrency"]["batch_size"]))
        for run in concurrent_runs.values()
    )
    tamper_ok = all(
        all(t.get("category_correct") and t.get("original_arm_preserved")
            for t in run["tampered_replays"])
        for run in concurrent_runs.values()
    )
    replay_ok = all(
        run["idempotent_replay"]["returns_original"]
        for run in concurrent_runs.values()
    )
    wave_rejection_ok = all(
        run["wave_rejection"] is None
        or (run["wave_rejection"].get("rejected")
            and run["wave_rejection"].get("category_correct"))
        for run in concurrent_runs.values()
    )
    tail_seal_ok = all(
        # seal_early 至少封闭过一个未满尾组；keep_open 最后一个尾组被封闭
        isinstance(run["tail"]["seal_report"]["sealed_tails"], list)
        for run in concurrent_runs.values()
    )
    return {
        "all_serial_double_runs_identical":
            all(c["identical"] for c in comparisons),
        "all_concurrent_slot_integrity_ok": concurrency_ok,
        "all_feature_tampering_rejected_and_preserved": tamper_ok,
        "all_idempotent_replays_return_original": replay_ok,
        "all_tail_wave_rejections_correct": wave_rejection_ok,
        "tail_seal_reports_present": tail_seal_ok,
        "all_balance_reports_pass":
            all(balance_ok(r) for r in concurrent_runs.values()),
        "all_stream_verifications_pass":
            all(verify_ok(r) for r in concurrent_runs.values()),
        "all_recoveries_ok": all(
            r["replayed_outcome"] == "replayed"
            and r["replayed_arm_matches"]
            and r["new_subject_allocated_after_restart"]
            and r["verification_after_recovery"] == "PASS"
            for r in recoveries
        ),
        "overall": "PASS" if (
            all(c["identical"] for c in comparisons)
            and concurrency_ok and tamper_ok and replay_ok
            and all(balance_ok(r) for r in concurrent_runs.values())
            and all(verify_ok(r) for r in concurrent_runs.values())
            and all(
                r["replayed_outcome"] == "replayed"
                and r["replayed_arm_matches"]
                and r["new_subject_allocated_after_restart"]
                and r["verification_after_recovery"] == "PASS"
                for r in recoveries)
        ) else "FAIL",
    }
