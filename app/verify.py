"""验证逻辑 —— 在验证者一侧运行，只依赖验证者持有的信任锚（公钥 + 可信检查点）。

判定（verdict）：
  VALID                      链完整、检查点签名有效、链头与最新可信锚一致
  VALID_UP_TO_ANCHOR         链完整且前缀与可信锚一致，但链尾超出锚的覆盖范围，
                             超出部分不可判定（锚之后被截尾无法发现）
  TAMPERED                   删改 / 重排 / 中间截断导致哈希链断裂，或链与锚矛盾
  TRUNCATED                  链自身完整，但可信锚（或日志自带检查点）证明了头上还有
                             更多记录 —— 可被判定的截尾
  FORGED_CHECKPOINT          检查点签名校验失败，或签名有效但内容与链不符
  CHAIN_VALID_NO_ANCHOR      链内部自洽，但验证者没有可用锚，整体真实性不可判定
"""

from __future__ import annotations

import base64
from typing import Any, Optional

from cryptography.exceptions import InvalidSignature

from .canonical import canonical_json, hash_object
from .chain import GENESIS_PREV_HASH
from .keys import load_public_key


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _sign_bytes(obj: Any) -> bytes:
    return canonical_json(obj)


def check_checkpoint_signature(
    checkpoint: dict, signature_b64: str, public_key_pem: str
) -> bool:
    try:
        key = load_public_key(public_key_pem)
        key.verify(_b64url_decode(signature_b64), _sign_bytes(checkpoint))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def check_chain(entries: list[dict]) -> Optional[dict]:
    """校验哈希链完整性。entries 为 [{"entry":..., "entry_hash":...}]。

    返回 None 表示完整；否则返回 {"position": i, "reason": ...}。
    可发现：单条删改、中间删除、重排、中间截断。
    """
    prev_hash = GENESIS_PREV_HASH
    expected_seq = 1
    for i, item in enumerate(entries):
        entry = item.get("entry")
        claimed = item.get("entry_hash")
        if not isinstance(entry, dict) or not isinstance(claimed, str):
            return {"position": i, "reason": "记录结构缺失 entry 或 entry_hash"}
        actual = hash_object(entry)
        if actual != claimed:
            return {
                "position": i,
                "reason": f"记录内容被删改：重算哈希 {actual} != 声明 {claimed}",
            }
        if entry.get("seq") != expected_seq:
            return {
                "position": i,
                "reason": f"序号不连续：期望 {expected_seq}，实际 {entry.get('seq')}",
            }
        if entry.get("prev_hash") != prev_hash:
            return {
                "position": i,
                "reason": "prev_hash 链接断裂（记录被删除、插入或重排）",
            }
        prev_hash = claimed
        expected_seq += 1
    return None


def verify_export(
    entries: list[dict],
    public_key_pem: str,
    checkpoint: Optional[dict] = None,
    checkpoint_signature: Optional[str] = None,
    trusted_checkpoint: Optional[dict] = None,
    trusted_signature: Optional[str] = None,
) -> dict:
    """验证一份导出区间。

    参数：
      entries             导出的 [{"entry":..., "entry_hash":...}]（应从创世开始，
                          或从某个被锚覆盖的前缀之后开始——本实现要求从创世开始）
      public_key_pem      验证者持有的公钥（信任锚，带外获得）
      checkpoint          日志方声称的最新检查点（不可信，需验签）
      trusted_checkpoint  验证者此前固定下来的可信检查点（锚）
    """
    result: dict[str, Any] = {
        "chain_valid": False,
        "chain_error": None,
        "checkpoint": {"present": checkpoint is not None},
        "anchor": {"present": trusted_checkpoint is not None},
        "verified_up_to_seq": 0,
        "verdict": None,
        "detail": "",
    }

    head_seq = entries[-1]["entry"]["seq"] if entries else 0
    head_hash = entries[-1]["entry_hash"] if entries else None

    # 1. 哈希链完整性
    err = check_chain(entries)
    if err is not None:
        result["chain_error"] = err
        result["verdict"] = "TAMPERED"
        result["detail"] = f"哈希链在第 {err['position']} 条处断裂：{err['reason']}"
        return result
    result["chain_valid"] = True
    result["verified_up_to_seq"] = head_seq  # 链自洽部分

    # 2. 日志方声称的检查点：验签 + 与链头比对
    if checkpoint is not None:
        cp = result["checkpoint"]
        if not checkpoint_signature:
            cp["signature_valid"] = False
            result["verdict"] = "FORGED_CHECKPOINT"
            result["detail"] = "检查点缺少签名"
            return result
        sig_ok = check_checkpoint_signature(
            checkpoint, checkpoint_signature, public_key_pem
        )
        cp["signature_valid"] = sig_ok
        if not sig_ok:
            result["verdict"] = "FORGED_CHECKPOINT"
            result["detail"] = "检查点签名校验失败（伪造或公钥不匹配）"
            return result
        cp_matches = (
            checkpoint.get("upto_seq") == head_seq
            and checkpoint.get("head_hash") == head_hash
        )
        cp["matches_log"] = cp_matches
        if not cp_matches:
            if checkpoint.get("upto_seq", 0) > head_seq:
                # 签名真实的检查点证明了头上还有记录 —— 可判定的截尾
                result["verdict"] = "TRUNCATED"
                result["verified_up_to_seq"] = head_seq
                result["detail"] = (
                    f"日志被截尾：有效签名的检查点证明存在到 seq="
                    f"{checkpoint['upto_seq']} 的记录，但导出只到 seq={head_seq}"
                )
            else:
                result["verdict"] = "TAMPERED"
                result["detail"] = "检查点与链头矛盾（链被改写后重新链接）"
            return result

    # 3. 与验证者持有的可信锚比对
    if trusted_checkpoint is None:
        result["verdict"] = "CHAIN_VALID_NO_ANCHOR"
        result["detail"] = (
            "哈希链内部自洽，但验证者未提供可信锚："
            "无法排除整条链被重算，尾部是否被截断亦不可判定"
        )
        return result

    anchor = result["anchor"]
    if not trusted_signature:
        anchor["signature_valid"] = False
        result["verdict"] = "FORGED_CHECKPOINT"
        result["detail"] = "可信锚缺少签名"
        return result
    anchor_ok = check_checkpoint_signature(
        trusted_checkpoint, trusted_signature, public_key_pem
    )
    anchor["signature_valid"] = anchor_ok
    if not anchor_ok:
        result["verdict"] = "FORGED_CHECKPOINT"
        result["detail"] = "可信锚签名校验失败（锚本身不可信）"
        return result

    a_seq = trusted_checkpoint["upto_seq"]
    a_hash = trusted_checkpoint["head_hash"]
    anchor["upto_seq"] = a_seq

    if a_seq > head_seq:
        # 锚证明头上还有更多记录 —— 可判定的截尾
        result["verdict"] = "TRUNCATED"
        result["verified_up_to_seq"] = head_seq
        result["detail"] = (
            f"日志被截尾：可信锚证明存在到 seq={a_seq} 的记录，"
            f"但导出只到 seq={head_seq}"
        )
        return result

    # 锚应落在链上（或正好等于链头）
    if a_seq == 0:
        anchor_on_chain = a_hash == GENESIS_PREV_HASH
    elif a_seq <= head_seq:
        anchor_on_chain = entries[a_seq - 1]["entry_hash"] == a_hash
    else:
        anchor_on_chain = False
    if not anchor_on_chain:
        result["verdict"] = "TAMPERED"
        result["verified_up_to_seq"] = 0
        result["detail"] = (
            f"链在可信锚位置 seq={a_seq} 处与锚不一致："
            "锚之前的记录被删改或整条链被重算"
        )
        return result

    if a_seq == head_seq:
        result["verdict"] = "VALID"
        result["verified_up_to_seq"] = head_seq
        result["detail"] = (
            "链完整且链头与最新可信锚一致。注意：锚之后若追加过记录再被删除，"
            "在没有更新的锚时不可判定。"
        )
    else:
        result["verdict"] = "VALID_UP_TO_ANCHOR"
        result["verified_up_to_seq"] = a_seq
        result["detail"] = (
            f"链在可信锚之前（seq<= {a_seq}）的部分已证实；"
            f"seq>{a_seq} 的尾部仅链内自洽、无锚覆盖，"
            "该尾部若被截断或改写，在获得更新的可信检查点前不可判定。"
        )
    return result
