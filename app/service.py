"""分配服务：编排契约、身份、随机内核与事务仓储。

这是唯一接触种子明文的应用层组件。所有写操作的边界是单个
``BEGIN IMMEDIATE`` 事务；并发登记因此串行提交，每个请求要么拿到
已存在的原分配（幂等回放），要么原子地占下一个槽位。
"""
from __future__ import annotations

import json
import sqlite3

from .contracts import (
    AllocationContract, build_contract, TAIL_SEAL_EARLY,
)
from .core.allocator import assign_at_position, lay_out_slots
from .core.identity import build_identity
from .core.seed import SecretSeed
from .core.vault import SeedVault
from .errors import AppError, ErrorCategory
from .storage.database import Database
from .storage.repository import Repository, utc_now_iso


class AllocationService:
    def __init__(self, db: Database, vault: SeedVault,
                 repo: Repository | None = None):
        self.db = db
        self.vault = vault
        self.repo = repo or Repository()

    # ================= 研究生命周期 =================
    def create_study(self, *, contract_kwargs: dict, seed: SecretSeed,
                     actor_role: str, request_id: str) -> dict:
        commitment = seed.commit()
        contract = build_contract(
            seed_fingerprint=commitment.fingerprint,
            seed_proof=commitment.proof,
            **contract_kwargs,
        )
        fingerprint = contract.fingerprint()
        with self.db.write_tx() as conn:
            self.repo.insert_study(conn, contract, fingerprint)
            self.repo.insert_audit(
                conn, request_id=request_id, study_id=contract.study_id,
                subject_id=None, actor_role=actor_role, action="study.create",
                outcome="committed",
                contract_fingerprint=fingerprint,
                detail={
                    "arms": list(contract.arm_ids),
                    "ratios": [a.ratio for a in contract.arms],
                    "factors": [f.name for f in contract.factors],
                    "block_size": contract.block_size,
                    "tail_policy": contract.tail_policy,
                    "seed_fingerprint": commitment.fingerprint,
                    "seed_proof_scheme": "pbkdf2-sha256",
                    "spec_version": contract.spec_version,
                    "contract_version": contract.version,
                },
            )
        # 种子入库在事务提交之后：绝不能出现"库无研究但种子已登记"
        self.vault.store(contract.study_id, seed)
        return {
            "study_id": contract.study_id,
            "contract_fingerprint": fingerprint,
            "block_size": contract.block_size,
            "arms": [
                {"arm_id": a.arm_id, "ratio": a.ratio,
                 "slots_per_block": a.ratio * contract.block_multiple}
                for a in contract.arms
            ],
            "tail_policy": contract.tail_policy,
            "spec_version": contract.spec_version,
            "version": contract.version,
        }

    def load_contract(self, conn: sqlite3.Connection, study_id: str) -> dict:
        row = self.repo.get_study_row(conn, study_id)
        payload = json.loads(row["contract_json"])
        contract = build_contract(
            study_id=payload["study_id"],
            arm_specs=[(a["arm_id"], a["ratio"]) for a in payload["arms"]],
            factor_specs=[(f["name"], f["levels"]) for f in payload["factors"]],
            block_multiple=payload["block_multiple"],
            tail_policy=payload["tail_policy"],
            seed_fingerprint=payload["seed_fingerprint"],
            seed_proof=row["seed_proof"],
            version=payload["version"],
        )
        return {"contract": contract, "row": row}

    # ================= 分配主流程 =================
    def allocate(self, *, study_id: str, subject_id: str, features: dict,
                 idempotency_key: str | None, actor_role: str,
                 request_id: str) -> dict:
        """返回分配结果描述。``outcome`` 区分 committed / replayed。"""
        with self.db.write_tx() as conn:
            loaded = self.load_contract(conn, study_id)
            contract: AllocationContract = loaded["contract"]
            if loaded["row"]["sealed"]:
                raise AppError(
                    ErrorCategory.ENROLLMENT_CLOSED, 409,
                    f"研究 {study_id!r} 已封闭，不再接收登记",
                )
            identity = build_identity(
                contract, subject_id=subject_id, features=features,
                idempotency_key=idempotency_key,
            )

            # ---- 1) 按 subject_id 查旧分配：同对象必须回原结果 ----
            existing = self.repo.get_allocation_by_subject(
                conn, study_id, identity.subject_id
            )
            if existing is not None:
                return self._handle_replay(
                    conn, existing=existing, identity=identity,
                    contract=contract, actor_role=actor_role,
                    request_id=request_id,
                )

            # ---- 2) 幂等键占用但对象不同：明确失败，不覆盖 ----
            if identity.idempotency_key is not None:
                other = self.repo.get_allocation_by_idem_key(
                    conn, study_id, identity.idempotency_key
                )
                if other is not None:
                    return self._conflict(
                        conn, contract=contract, identity=identity,
                        actor_role=actor_role, request_id=request_id,
                        category=ErrorCategory.IDEMPOTENCY_KEY_REUSE_CONFLICT,
                        message="幂等键已绑定另一个对象",
                        detail={"bound_subject_id": other["subject_id"]},
                    )

            # ---- 3) 尾组策略门：seal_early 下不允许新开区组 ----
            self._enforce_tail_policy(conn, contract, identity.stratum_key)

            # ---- 4) 取/建开放区组并占下一个位置（串行事务内无竞争） ----
            self.repo.ensure_stratum(conn, study_id, identity.stratum_key)
            block_row = self.repo.get_open_block(conn, study_id,
                                                 identity.stratum_key)
            created_block = False
            if block_row is None:
                block_index = self.repo.next_block_index(
                    conn, study_id, identity.stratum_key
                )
                block_id = self.repo.insert_block(
                    conn, study_id, identity.stratum_key, block_index,
                    contract.block_size, list(lay_out_slots(contract)),
                )
                block_row = conn.execute(
                    "SELECT * FROM blocks WHERE id=?", (block_id,)
                ).fetchone()
                created_block = True

            position = self.repo.claim_next_slot(conn, block_row["id"])
            block_index = int(block_row["block_index"])
            block_id = int(block_row["id"])

            # ---- 5) 用种子纯函数重算该位置的臂 ----
            seed = self.vault.get(study_id)
            decision = assign_at_position(
                contract, seed.material, identity.stratum_key,
                block_index, position,
            )
            became_full = position + 1 == contract.block_size
            if became_full:
                # 区组排满：次序已全部消费，落档置换供审计直接读取
                self.repo.record_block_permutation(
                    conn, block_id, decision["permutation"]
                )

            now = utc_now_iso()
            self.repo.insert_allocation(
                conn,
                study_id=study_id,
                subject_id=identity.subject_id,
                stratum_key=identity.stratum_key,
                block_id=block_id,
                position=position,
                arm=decision["arm"],
                features_digest=identity.features_digest,
                features_canonical=identity.features_canonical,
                idempotency_key=identity.idempotency_key,
                request_id=request_id,
                created_at=now,
            )
            # 本次新消耗的随机决定只需要记录到当前 position 为止
            used_locators = [
                loc.as_dict() for loc in decision["locators"][:position + 1]
            ]
            self.repo.insert_audit(
                conn, request_id=request_id, study_id=study_id,
                subject_id=identity.subject_id, actor_role=actor_role,
                action="allocate", outcome="committed",
                contract_fingerprint=contract.fingerprint(),
                stream_locators=used_locators,
                detail={
                    "arm": decision["arm"],
                    "stratum_key": identity.stratum_key,
                    "block_index": block_index,
                    "position": position,
                    "slot": decision["slot"],
                    "block_size": contract.block_size,
                    "block_opened_on_this_request": created_block,
                    "block_became_full": became_full,
                    "tail_policy": contract.tail_policy,
                    "features_digest": identity.features_digest,
                    "idempotency_key_present": identity.idempotency_key is not None,
                    "random_source": {
                        "prf": "HMAC-SHA256",
                        "sampling": "unbiased rejection (randbelow)",
                        "shuffle": "Fisher-Yates",
                        "seed_fingerprint": contract.seed_fingerprint,
                        "domain": "rct/v1/block-permutation",
                    },
                },
            )
            return {
                "outcome": "committed",
                "study_id": study_id,
                "subject_id": identity.subject_id,
                "arm": decision["arm"],
                "stratum_key": identity.stratum_key,
                "block_index": block_index,
                "position": position,
                "slot": decision["slot"],
                "block_became_full": became_full,
                "contract_fingerprint": contract.fingerprint(),
                "spec_version": contract.spec_version,
                "version": contract.version,
                "request_id": request_id,
                "created_at": now,
            }

    # ================= 回放与冲突 =================
    def _handle_replay(self, conn, *, existing, identity, contract,
                       actor_role, request_id) -> dict:
        if existing["features_canonical"] != identity.features_canonical:
            return self._conflict(
                conn, contract=contract, identity=identity,
                actor_role=actor_role, request_id=request_id,
                category=ErrorCategory.FEATURES_CHANGED_AFTER_ALLOCATION,
                message="对象已存在分配但本次分层特征与首次不同；"
                        "系统保留原分配，不会重新随机",
                detail={
                    "original_features_digest": existing["features_digest"],
                    "submitted_features_digest": identity.features_digest,
                    "original_stratum_key": existing["stratum_key"],
                },
            )
        # 特征一致：幂等键也必须一致（若双方都提供）
        if (existing["idempotency_key"] is not None
                and identity.idempotency_key is not None
                and existing["idempotency_key"] != identity.idempotency_key):
            return self._conflict(
                conn, contract=contract, identity=identity,
                actor_role=actor_role, request_id=request_id,
                category=ErrorCategory.IDEMPOTENCY_KEY_MISMATCH,
                message="同一对象使用了与首次不同的幂等键",
                detail={"bound_key": existing["idempotency_key"]},
            )
        self.repo.insert_audit(
            conn, request_id=request_id, study_id=identity.study_id,
            subject_id=identity.subject_id, actor_role=actor_role,
            action="allocate", outcome="replayed",
            contract_fingerprint=contract.fingerprint(),
            detail={
                "arm": existing["arm"],
                "stratum_key": existing["stratum_key"],
                "block_index": self._block_index_of(conn, existing["block_id"]),
                "position": existing["position"],
                "original_request_id": existing["request_id"],
                "note": "重复请求返回原分配，未消耗随机数",
            },
        )
        return {
            "outcome": "replayed",
            "study_id": identity.study_id,
            "subject_id": identity.subject_id,
            "arm": existing["arm"],
            "stratum_key": existing["stratum_key"],
            "block_index": self._block_index_of(conn, existing["block_id"]),
            "position": existing["position"],
            "block_became_full": False,
            "contract_fingerprint": contract.fingerprint(),
            "spec_version": contract.spec_version,
            "version": contract.version,
            "request_id": request_id,
            "created_at": existing["created_at"],
            "original_request_id": existing["request_id"],
        }

    def _conflict(self, conn, *, contract, identity, actor_role, request_id,
                  category: str, message: str, detail: dict) -> dict:
        """冲突也落审计（outcome=rejected），然后抛稳定类别。"""
        self.repo.insert_audit(
            conn, request_id=request_id, study_id=identity.study_id,
            subject_id=identity.subject_id, actor_role=actor_role,
            action="allocate", outcome="rejected", category=category,
            contract_fingerprint=contract.fingerprint(),
            detail=detail,
        )
        raise AppError(category, 409, message, detail)

    def _block_index_of(self, conn: sqlite3.Connection, block_id: int) -> int:
        row = conn.execute(
            "SELECT block_index FROM blocks WHERE id=?", (block_id,)
        ).fetchone()
        return int(row["block_index"])

    def _enforce_tail_policy(self, conn, contract, stratum_key: str) -> None:
        if contract.tail_policy != TAIL_SEAL_EARLY:
            return
        # seal_early：只允许在*已有且未满*的区组中登记；没有开放区组
        # （尾组已封闭或尚未开组）都拒绝新开。
        open_block = self.repo.get_open_block(
            conn, contract.study_id, stratum_key
        )
        if open_block is None:
            raise AppError(
                ErrorCategory.STRATUM_SEALED, 409,
                "该分层采用 seal_early 尾组策略且当前无开放区组，"
                "不能为补满尾组而新开区组",
                {"stratum_key": stratum_key, "tail_policy": TAIL_SEAL_EARLY},
            )

    # ================= 研究封闭 =================
    def seal_study(self, *, study_id: str, actor_role: str,
                   request_id: str) -> dict:
        """整体停止入组（不可逆）。已分配结果保留，新登记被拒绝。"""
        with self.db.write_tx() as conn:
            self.load_contract(conn, study_id)
            conn.execute(
                "UPDATE studies SET sealed=1 WHERE study_id=?", (study_id,))
            self.repo.insert_audit(
                conn, request_id=request_id, study_id=study_id,
                subject_id=None, actor_role=actor_role,
                action="study.seal", outcome="committed")
            return {"study_id": study_id, "sealed": True}

    # ================= 显式开组（seal_early） =================
    def open_next_block(self, *, study_id: str, stratum_key: str,
                       actor_role: str, request_id: str) -> dict:
        """seal_early 策略下由管理员显式开出下一个空区组。

        这保证"开组"是一个带审计的决定：系统绝不自行在尾组之后补开。
        """
        with self.db.write_tx() as conn:
            loaded = self.load_contract(conn, study_id)
            contract: AllocationContract = loaded["contract"]
            # 校验层键合法（不依赖具体特征值，只校验结构）
            if contract.factors:
                parts = stratum_key.split("|")
                if len(parts) != len(contract.factors):
                    raise AppError(
                        ErrorCategory.REQUEST_VALIDATION_FAILED, 422,
                        f"stratum_key 应有 {len(contract.factors)} 段",
                    )
                for factor, value in zip(contract.factors, parts):
                    if value not in factor.levels:
                        raise AppError(
                            ErrorCategory.NON_DISCRETE_STRATUM_VALUE, 422,
                            f"因子 {factor.name!r} 的取值 {value!r} 不在枚举层内",
                        )
            open_block = self.repo.get_open_block(conn, study_id, stratum_key)
            if open_block is not None:
                raise AppError(
                    ErrorCategory.OPEN_TAIL_BLOCK, 409,
                    "该层已存在开放区组；seal_early 下必须先封闭尾组才能"
                    "再开新组",
                    {"stratum_key": stratum_key,
                     "open_block_index": int(open_block["block_index"])},
                )
            self.repo.ensure_stratum(conn, study_id, stratum_key)
            conn.execute(
                "UPDATE strata SET status='open', sealed_at=NULL "
                "WHERE study_id=? AND stratum_key=?",
                (study_id, stratum_key),
            )
            block_index = self.repo.next_block_index(conn, study_id, stratum_key)
            block_id = self.repo.insert_block(
                conn, study_id, stratum_key, block_index,
                contract.block_size, list(lay_out_slots(contract)),
            )
            self.repo.insert_audit(
                conn, request_id=request_id, study_id=study_id,
                subject_id=None, actor_role=actor_role,
                action="block.open", outcome="committed",
                contract_fingerprint=contract.fingerprint(),
                detail={"stratum_key": stratum_key, "block_index": block_index,
                        "block_size": contract.block_size,
                        "tail_policy": contract.tail_policy},
            )
            return {"study_id": study_id, "stratum_key": stratum_key,
                    "block_index": block_index, "block_id": block_id,
                    "block_size": contract.block_size}

    # ================= 尾组封闭 =================
    def seal_tails(self, *, study_id: str, actor_role: str,
                   request_id: str) -> dict:
        with self.db.write_tx() as conn:
            loaded = self.load_contract(conn, study_id)
            contract: AllocationContract = loaded["contract"]
            report = []
            for stratum_row in self.repo.list_strata(conn, study_id):
                sk = stratum_row["stratum_key"]
                open_block = self.repo.get_open_block(conn, study_id, sk)
                if open_block is None:
                    continue
                block_id = int(open_block["id"])
                filled = int(open_block["filled"])
                if filled == int(open_block["block_size"]):
                    continue  # 已满的区组状态已为 full
                # 尾组未满即封闭：记录置换留档（种子仍可复核），
                # 状态置 sealed_tail，分层置 sealed。
                seed = self.vault.get(study_id)
                perm, _ = self._permutation_for(
                    contract, seed.material, sk, int(open_block["block_index"])
                )
                self.repo.record_block_permutation(conn, block_id, perm)
                ids = [block_id]
                conn.execute(
                    "UPDATE blocks SET status='sealed_tail' WHERE id=?",
                    (block_id,),
                )
                self.repo.seal_stratum(conn, study_id, sk)
                report.append({
                    "stratum_key": sk,
                    "block_index": int(open_block["block_index"]),
                    "filled": filled,
                    "block_size": int(open_block["block_size"]),
                    "missing": int(open_block["block_size"]) - filled,
                })
            self.repo.insert_audit(
                conn, request_id=request_id, study_id=study_id,
                subject_id=None, actor_role=actor_role,
                action="tails.seal",
                outcome="committed" if report else "replayed",
                contract_fingerprint=contract.fingerprint(),
                detail={"sealed_tails": report, "tail_policy": contract.tail_policy},
            )
            return {"study_id": study_id, "sealed_tails": report}

    def _permutation_for(self, contract, seed_material, stratum_key, block_index):
        from .core.stream import block_permutation
        return block_permutation(
            seed_material, contract.study_id, contract.fingerprint(),
            stratum_key, block_index, contract.block_size,
        )

    # ================= 查询（不泄密） =================
    def get_assignment(self, *, study_id: str, subject_id: str,
                       actor_role: str, request_id: str) -> dict:
        with self.db.connection() as conn:
            loaded = self.load_contract(conn, study_id)
            contract = loaded["contract"]
            row = self.repo.get_allocation_by_subject(conn, study_id, subject_id)
            if row is None:
                raise AppError(
                    ErrorCategory.SUBJECT_NOT_FOUND, 404,
                    f"对象 {subject_id!r} 在研究 {study_id!r} 中尚无分配",
                )
            self.repo.insert_audit(
                conn, request_id=request_id, study_id=study_id,
                subject_id=subject_id, actor_role=actor_role,
                action="assignment.lookup", outcome="committed",
                contract_fingerprint=contract.fingerprint(),
                detail={"revealed": True},
            )
            conn.commit()  # 读连接上的审计写入需要显式提交
            return {
                "study_id": study_id,
                "subject_id": subject_id,
                "arm": row["arm"],
                "outcome": "existing",
                "stratum_key": row["stratum_key"],
                "block_index": self._block_index_of(conn, row["block_id"]),
                "position": row["position"],
                "contract_fingerprint": contract.fingerprint(),
                "spec_version": contract.spec_version,
                "version": contract.version,
                "request_id": request_id,
            }
