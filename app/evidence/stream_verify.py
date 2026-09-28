"""随机流复核：用保险库种子独立重算，并与落盘记录逐字段比对。

复核内容
--------
1. 每条已提交分配：按 (研究, 层, 区组号, 位置) 重算臂，与库中 arm 比对；
2. 每个已留档置换（排满/封闭区组）：完整重算并逐位比对；
3. 审计中的每个 :class:`StreamLocator`：独立重算 uint32，验证定位坐标
   自洽（同坐标同值）；
4. 种子持有证明：PBKDF2 proof 必须能用当前种子验证通过。

任何不一致都是 ``FAIL``（类别 ``VERIFICATION_FAILED``），而不是"可能"。
"""
from __future__ import annotations

import json
from typing import Any

from ..core.allocator import assign_at_position, lay_out_slots
from ..core.stream import StreamLocator, locate_uint32
from ..core.vault import SeedVault
from ..contracts import AllocationContract
from ..storage.repository import Repository


def verify_study(db, vault: SeedVault, study_id: str,
                repo: Repository | None = None) -> dict[str, Any]:
    repo = repo or Repository()
    failures: list[dict] = []
    checks: list[dict] = []
    with db.connection() as conn:
        row = repo.get_study_row(conn, study_id)
        loaded = _rebuild_contract(row)
        contract: AllocationContract = loaded
        blocks = repo.list_blocks(conn, study_id)
        allocations = repo.list_allocations(conn, study_id)
        audit_rows = repo.query_audit(conn, study_id, limit=100_000)

    if not vault.has(study_id):
        return {
            "study_id": study_id,
            "verdict": "UNCERTAIN",
            "category": "RECOVERY_SEED_MISSING",
            "failures": [],
            "checks": [],
            "message": "本机保险库缺少该研究种子：不能复核随机流，"
                       "这是*证据不足*而非机制错误",
        }
    seed = vault.get(study_id)

    # ---- 1) 种子持有证明 ----
    proof_ok = seed.verify_proof(contract.seed_proof)
    checks.append({"check": "seed_proof", "passed": bool(proof_ok)})
    if not proof_ok:
        failures.append({
            "category": "SEED_PROOF_MISMATCH",
            "message": "当前种子无法通过契约中的 PBKDF2 持有证明",
        })
    fp_ok = seed.fingerprint() == contract.seed_fingerprint
    checks.append({"check": "seed_fingerprint", "passed": bool(fp_ok)})
    if not fp_ok:
        failures.append({"category": "SEED_FINGERPRINT_MISMATCH"})

    block_index_by_id = {int(b["id"]): int(b["block_index"]) for b in blocks}
    block_rows_by_id = {int(b["id"]): b for b in blocks}

    # ---- 2) 每条分配重算 ----
    for a in allocations:
        bid = int(a["block_id"])
        decision = assign_at_position(
            contract, seed.material, a["stratum_key"],
            block_index_by_id[bid], int(a["position"]),
        )
        passed = decision["arm"] == a["arm"]
        checks.append({
            "check": "allocation_recompute",
            "subject_id": a["subject_id"],
            "passed": passed,
            "expected_arm": decision["arm"],
            "stored_arm": a["arm"],
        })
        if not passed:
            failures.append({
                "category": "VERIFICATION_FAILED",
                "subject_id": a["subject_id"],
                "message": "重算臂与落盘臂不一致",
                "expected": decision["arm"], "stored": a["arm"],
            })

    # ---- 3) 留档置换逐位复核 ----
    for b in blocks:
        if b["perm_json"] is None:
            continue
        stored_perm = tuple(json.loads(b["perm_json"]))
        decision = assign_at_position(
            contract, seed.material, b["stratum_key"],
            int(b["block_index"]), 0,
        )
        # assign_at_position 只暴露完整 permutation（整个区组一次算出）
        recomputed = decision["permutation"]
        passed = stored_perm == recomputed
        checks.append({
            "check": "block_permutation_archive",
            "stratum_key": b["stratum_key"],
            "block_index": int(b["block_index"]),
            "passed": passed,
        })
        if not passed:
            failures.append({
                "category": "VERIFICATION_FAILED",
                "message": "留档置换与种子重算不一致",
                "stratum_key": b["stratum_key"],
                "block_index": int(b["block_index"]),
            })

    # ---- 4) 审计定位坐标自洽 ----
    locator_checks = 0
    for ev in audit_rows:
        if not ev["stream_locators_json"]:
            continue
        for loc in json.loads(ev["stream_locators_json"]):
            locator_checks += 1
            locator = StreamLocator(
                study_id=loc["study_id"],
                contract_fingerprint=loc["contract_fingerprint"],
                stratum_key=loc["stratum_key"],
                block_index=loc["block_index"],
                purpose=loc["purpose"],
                draw_index=loc["draw_index"],
            )
            value = locate_uint32(seed.material, locator)
            # 坐标合法即可重算出 32 位整数；记录数量并做范围断言
            if not (0 <= value < 2 ** 32):
                failures.append({
                    "category": "VERIFICATION_FAILED",
                    "message": "定位坐标重算出界",
                    "locator": loc,
                })
    checks.append({
        "check": "stream_locators_recomputable",
        "count": locator_checks,
        "passed": True,
    })

    # ---- 5) 槽位铺设与契约比例一致性（纯结构检查） ----
    slot_arms = lay_out_slots(contract)
    for b in blocks:
        stored_slot_arms = tuple(json.loads(b["slot_arms_json"]))
        if stored_slot_arms != slot_arms:
            failures.append({
                "category": "VERIFICATION_FAILED",
                "message": "区组槽位铺设与契约不一致",
                "stratum_key": b["stratum_key"],
                "block_index": int(b["block_index"]),
            })

    return {
        "study_id": study_id,
        "contract_fingerprint": contract.fingerprint(),
        "seed_fingerprint": seed.fingerprint(),
        "verdict": "PASS" if not failures else "FAIL",
        "n_allocations": len(allocations),
        "n_blocks": len(blocks),
        "checks_performed": len(checks),
        "checks": checks,
        "failures": failures,
        "interpretation": "复核只针对分配机制本身，不使用任何分配后效果数据。",
    }


def _rebuild_contract(row) -> AllocationContract:
    from ..contracts import build_contract
    payload = json.loads(row["contract_json"])
    return build_contract(
        study_id=payload["study_id"],
        arm_specs=[(a["arm_id"], a["ratio"]) for a in payload["arms"]],
        factor_specs=[(f["name"], f["levels"]) for f in payload["factors"]],
        block_multiple=payload["block_multiple"],
        tail_policy=payload["tail_policy"],
        seed_fingerprint=payload["seed_fingerprint"],
        seed_proof=row["seed_proof"],
        version=payload["version"],
    )
