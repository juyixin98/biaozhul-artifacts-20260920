"""分层身份与特征冻结。

身份锚点是调用方提供的稳定 ``subject_id``；分层特征在*首次分配时*被
规范化并哈希固化。之后同一 subject_id 再次请求：

- 特征一致      → 回原分配（幂等），不消耗任何随机数；
- 特征不一致    → 409 ``FEATURES_CHANGED_AFTER_ALLOCATION``，绝不重新随机；
- 幂等键冲突    → 409，区分"同键不同体"与"同体不同键"两类失败。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Mapping


def canonical_features(features: Mapping[str, str]) -> bytes:
    """特征的规范序列化（键排序），用于哈希与逐字节比较。"""
    return json.dumps(
        {k: features[k] for k in sorted(features)},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")


def features_digest(features: Mapping[str, str]) -> str:
    return "feat_" + hashlib.sha256(canonical_features(features)).hexdigest()


@dataclass(frozen=True)
class SubjectIdentity:
    study_id: str
    subject_id: str
    stratum_key: str
    features_digest: str
    features_canonical: bytes
    idempotency_key: str | None


def build_identity(contract, *, subject_id: str,
                   features: Mapping[str, str],
                   idempotency_key: str | None) -> SubjectIdentity:
    if not isinstance(subject_id, str) or not subject_id.strip():
        from ..errors import AppError, ErrorCategory
        raise AppError(
            ErrorCategory.REQUEST_VALIDATION_FAILED, 422, "subject_id 不能为空"
        )
    if len(subject_id) > 128:
        from ..errors import AppError, ErrorCategory
        raise AppError(
            ErrorCategory.REQUEST_VALIDATION_FAILED, 422,
            "subject_id 长度不能超过 128",
        )
    if idempotency_key is not None:
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            from ..errors import AppError, ErrorCategory
            raise AppError(
                ErrorCategory.REQUEST_VALIDATION_FAILED, 422,
                "Idempotency-Key 不能为空字符串（省略该头即可）",
            )
        if len(idempotency_key) > 128:
            from ..errors import AppError, ErrorCategory
            raise AppError(
                ErrorCategory.REQUEST_VALIDATION_FAILED, 422,
                "Idempotency-Key 长度不能超过 128",
            )
    contract.validate_features(features)
    canonical = canonical_features(features)
    return SubjectIdentity(
        study_id=contract.study_id,
        subject_id=subject_id.strip(),
        stratum_key=contract.stratum_key(features),
        features_digest="feat_" + hashlib.sha256(canonical).hexdigest(),
        features_canonical=canonical,
        idempotency_key=idempotency_key.strip() if idempotency_key else None,
    )
