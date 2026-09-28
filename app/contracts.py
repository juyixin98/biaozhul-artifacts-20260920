"""统计契约（statistical contract）。

契约对象是*纯值*：一旦 ``freeze()`` 即不可变，并带有内容指纹。内核、
存储、审计都以契约指纹为锚点——同一研究的所有分配必然落在同一版本上。

本模块只做声明与校验，不含随机机制；随机机制属于 :mod:`app.core`。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Mapping

from .errors import AppError, ErrorCategory

CONTRACT_SPEC_VERSION = "contract/v1"

# 尾组策略（最后一个未填满区组）：
TAIL_KEEP_OPEN = "keep_open"        # 保留为开放区组，后续登记继续填入（默认）
TAIL_SEAL_EARLY = "seal_early"      # 主动封闭：该层停止接收入组（计数实验用）

VALID_TAIL_POLICIES = frozenset({TAIL_KEEP_OPEN, TAIL_SEAL_EARLY})
MAX_ARMS = 12
MAX_FACTORS = 8
MAX_LEVELS_PER_FACTOR = 64
MAX_BLOCK_MULTIPLE = 100
MAX_SUBJECT_ID_LEN = 128
MAX_KEY_LEN = 64


def _canonical_json(obj) -> bytes:
    """确定性 JSON：键排序、无空白、UTF-8。"""
    return json.dumps(
        obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=_fail_default
    ).encode("utf-8")


def _fail_default(o):  # pragma: no cover - 仅防御
    raise TypeError(f"不可序列化的契约成员: {type(o)!r}")


@dataclass(frozen=True)
class Arm:
    arm_id: str
    ratio: int  # 正整数；实际比例 = 各臂权重之比


@dataclass(frozen=True)
class StratumFactor:
    """分层因子。``levels`` 显式枚举——不允许自由文本层，防止同一层的
    不同拼写静默分裂成两层（这是分层随机常见的实现事故）。"""

    name: str
    levels: tuple[str, ...]


@dataclass(frozen=True)
class AllocationContract:
    study_id: str
    arms: tuple[Arm, ...]
    factors: tuple[StratumFactor, ...]
    block_multiple: int
    tail_policy: str
    seed_fingerprint: str          # 见 core.seed.SecretSeed.fingerprint
    seed_proof: str                # 形如 "pbkdf2-sha256$iter$salt_b64$hash_b64"
    spec_version: str = CONTRACT_SPEC_VERSION
    version: int = 1
    created_at: str = ""

    # ---- 派生量 ----
    @property
    def arm_ids(self) -> tuple[str, ...]:
        return tuple(a.arm_id for a in self.arms)

    @property
    def ratio_sum(self) -> int:
        return sum(a.ratio for a in self.arms)

    @property
    def block_size(self) -> int:
        """区组长度 = 比例和 × 区组乘子（恒为比例和的整数倍）。"""
        return self.block_multiple * self.ratio_sum

    def arm_slots_per_block(self) -> dict[str, int]:
        return {a.arm_id: a.ratio * self.block_multiple for a in self.arms}

    def canonical_payload(self) -> bytes:
        # 注意：seed_proof *不*进入指纹。proof 是带随机盐的持有证明
        # （每次可重新签发），若进入指纹会使同一研究在不同进程/重放中
        # 指纹漂移，进而改变随机流。种子的密码学绑定由确定性的
        # seed_fingerprint 承担。
        return _canonical_json(
            {
                "spec_version": self.spec_version,
                "study_id": self.study_id,
                "arms": [{"arm_id": a.arm_id, "ratio": a.ratio} for a in self.arms],
                "factors": [
                    {"name": f.name, "levels": list(f.levels)} for f in self.factors
                ],
                "block_multiple": self.block_multiple,
                "tail_policy": self.tail_policy,
                "seed_fingerprint": self.seed_fingerprint,
                "version": self.version,
            }
        )

    def fingerprint(self) -> str:
        """契约内容指纹（SHA-256, hex）。任何字段变化都会改变指纹。"""
        return "ctr_" + hashlib.sha256(self.canonical_payload()).hexdigest()

    def with_created_at(self, created_at: str) -> "AllocationContract":
        return replace(self, created_at=created_at)

    # ---- 分层校验 ----
    def validate_features(self, features: Mapping[str, str]) -> None:
        """校验登记特征与契约声明的因子完全一致且取值在枚举内。"""
        declared = {f.name for f in self.factors}
        provided = set(features)
        missing = declared - provided
        if missing:
            raise AppError(
                ErrorCategory.MISSING_STRATUM_FACTOR,
                422,
                f"缺少分层因子: {sorted(missing)}",
                {"missing": sorted(missing)},
            )
        unexpected = provided - declared
        if unexpected:
            raise AppError(
                ErrorCategory.UNEXPECTED_STRATUM_FACTOR,
                422,
                f"出现契约未声明的分层因子: {sorted(unexpected)}",
                {"unexpected": sorted(unexpected)},
            )
        levels_by_name = {f.name: set(f.levels) for f in self.factors}
        for name in declared:
            value = features[name]
            if not isinstance(value, str):
                raise AppError(
                    ErrorCategory.NON_DISCRETE_STRATUM_VALUE,
                    422,
                    f"分层因子 {name!r} 的取值必须是离散字符串",
                    {"factor": name, "value": repr(value)},
                )
            if value == "":
                raise AppError(
                    ErrorCategory.EMPTY_STRATUM_VALUE,
                    422,
                    f"分层因子 {name!r} 的取值不能为空",
                    {"factor": name},
                )
            if value not in levels_by_name[name]:
                raise AppError(
                    ErrorCategory.NON_DISCRETE_STRATUM_VALUE,
                    422,
                    f"分层因子 {name!r} 的取值 {value!r} 不在枚举层内",
                    {"factor": name, "value": value,
                     "allowed": sorted(levels_by_name[name])},
                )

    def stratum_key(self, features: Mapping[str, str]) -> str:
        """特征 → 规范分层键（按契约中因子顺序取值）。"""
        return "|".join(str(features[f.name]) for f in self.factors)


def build_contract(
    *,
    study_id: str,
    arm_specs: list[tuple[str, int] | dict],
    factor_specs: list[tuple[str, list[str]]] | None,
    block_multiple: int,
    tail_policy: str,
    seed_fingerprint: str,
    seed_proof: str,
    version: int = 1,
    created_at: str = "",
) -> AllocationContract:
    """构造并*严格校验*契约。任何统计上无意义的输入在此被拒绝。"""
    problems: list[str] = []

    if not isinstance(study_id, str) or not study_id.strip():
        problems.append("study_id 不能为空")
    elif len(study_id) > MAX_KEY_LEN:
        problems.append(f"study_id 长度不能超过 {MAX_KEY_LEN}")

    arms = _build_arms(arm_specs, problems)
    factors = _build_factors(factor_specs or [], problems)

    if not isinstance(block_multiple, int) or isinstance(block_multiple, bool):
        problems.append("block_multiple 必须是正整数")
    elif not (1 <= block_multiple <= MAX_BLOCK_MULTIPLE):
        problems.append(f"block_multiple 必须在 1..{MAX_BLOCK_MULTIPLE} 之间")

    if tail_policy not in VALID_TAIL_POLICIES:
        problems.append(
            f"tail_policy 必须是 {sorted(VALID_TAIL_POLICIES)} 之一"
        )

    if not seed_fingerprint.startswith("seed_"):
        problems.append("seed_fingerprint 格式非法")
    if not seed_proof:
        problems.append("seed_proof 不能为空")

    if problems:
        raise AppError(
            ErrorCategory.INVALID_CONTRACT,
            422,
            "分配契约校验失败: " + "; ".join(problems),
            {"problems": problems},
        )

    return AllocationContract(
        study_id=study_id.strip(),
        arms=arms,
        factors=factors,
        block_multiple=block_multiple,
        tail_policy=tail_policy,
        seed_fingerprint=seed_fingerprint,
        seed_proof=seed_proof,
        version=version,
        created_at=created_at,
    )


def _build_arms(arm_specs, problems) -> tuple[Arm, ...]:
    if not isinstance(arm_specs, list) or len(arm_specs) < 2:
        problems.append("至少需要 2 个处理臂")
        return ()
    arms: list[Arm] = []
    seen: set[str] = set()
    for spec in arm_specs:
        if isinstance(spec, dict):
            arm_id, ratio = spec.get("arm_id"), spec.get("ratio")
        else:
            arm_id, ratio = spec
        if not isinstance(arm_id, str) or not arm_id.strip():
            problems.append(f"臂标识非法: {arm_id!r}")
            continue
        if arm_id in seen:
            problems.append(f"臂标识重复: {arm_id!r}")
            continue
        seen.add(arm_id)
        if not isinstance(ratio, int) or isinstance(ratio, bool) or ratio < 1:
            problems.append(f"臂 {arm_id!r} 的比例必须是正整数")
            continue
        if len(arms) >= MAX_ARMS:
            problems.append(f"处理臂数量不能超过 {MAX_ARMS}")
            break
        arms.append(Arm(arm_id=arm_id.strip(), ratio=ratio))
    return tuple(arms)


def _build_factors(factor_specs, problems) -> tuple[StratumFactor, ...]:
    if not isinstance(factor_specs, list):
        problems.append("factors 必须是列表")
        return ()
    factors: list[StratumFactor] = []
    seen_names: set[str] = set()
    for spec in factor_specs:
        if isinstance(spec, dict):
            name, levels = spec.get("name"), spec.get("levels")
        else:
            name, levels = spec
        if not isinstance(name, str) or not name.strip():
            problems.append(f"分层因子名非法: {name!r}")
            continue
        if name in seen_names:
            problems.append(f"分层因子名重复: {name!r}")
            continue
        seen_names.add(name)
        if not isinstance(levels, list) or len(levels) < 1:
            problems.append(f"因子 {name!r} 至少需要 1 个枚举层")
            continue
        if len(levels) > MAX_LEVELS_PER_FACTOR:
            problems.append(
                f"因子 {name!r} 的层数不能超过 {MAX_LEVELS_PER_FACTOR}"
            )
            continue
        clean_levels: list[str] = []
        seen_levels: set[str] = set()
        bad = False
        for lv in levels:
            if not isinstance(lv, str) or not lv.strip():
                problems.append(f"因子 {name!r} 存在空层名")
                bad = True
                break
            if lv in seen_levels:
                problems.append(f"因子 {name!r} 层名重复: {lv!r}")
                bad = True
                break
            seen_levels.add(lv)
            clean_levels.append(lv)
        if not bad:
            factors.append(StratumFactor(name=name.strip(), levels=tuple(clean_levels)))
        if len(factors) >= MAX_FACTORS:
            problems.append(f"分层因子数量不能超过 {MAX_FACTORS}")
            break
    return tuple(factors)
