# -*- coding: utf-8 -*-
"""检查点与状态证明的密码学验证。

信任链条（全部真实执行）：
  对端链私钥签名检查点
    -> 本地用创建客户端时登记的公钥验 Ed25519 签名（信任根）
    -> 检查新鲜度（高度单调、在信任期内）
    -> 用检查点 app_hash 作为状态根验证 Merkle 存在性/不存在性证明

任何一环失败都拒绝并返回明确错误码。
"""
from __future__ import annotations

from typing import Optional

from . import crypto
from .encoding import from_hex
from .errors import (
    ERR_BAD_INPUT,
    ERR_BAD_PROOF_STRUCTURE,
    ERR_INVALID_CHECKPOINT,
    ERR_PROOF_KEY,
    ERR_PROOF_VALUE,
    ERR_STALE_CHECKPOINT,
    AppError,
)
from .smt import SparseMerkleTree

# 检查点里必须存在的字段
_CP_FIELDS = ("chain_id", "revision_number", "height", "time_nanos", "app_hash", "signature", "version", "seq")


def _cp_object(p: dict) -> dict:
    cp = p.get("checkpoint")
    if not isinstance(cp, dict):
        raise AppError(ERR_BAD_PROOF_STRUCTURE, "证明缺少已签名检查点 checkpoint", 400)
    for f in _CP_FIELDS:
        if f not in cp:
            raise AppError(ERR_BAD_PROOF_STRUCTURE, f"检查点缺少字段 {f}", 400)
    return cp


def verify_checkpoint(
    p: dict,
    trusted_pubkey_hex: str,
    expected_chain_id: str,
    last_trusted_height: int,
    last_trusted_time: int,
    trusting_period_nanos: int,
    *,
    now_nanos: Optional[int] = None,
) -> dict:
    """验证检查点签名与新鲜度，返回规范化的检查点字段。"""
    cp = _cp_object(p)
    if cp.get("chain_id") != expected_chain_id:
        raise AppError(
            ERR_INVALID_CHECKPOINT,
            f"检查点链 {cp.get('chain_id')} 与客户端信任链 {expected_chain_id} 不一致",
            400,
        )
    if cp.get("version") != crypto.CHECKPOINT_VERSION:
        raise AppError(ERR_INVALID_CHECKPOINT, "检查点版本不受支持", 400)

    try:
        sig = from_hex(cp["signature"], "signature")
        app_hash = from_hex(cp["app_hash"], "app_hash")
    except ValueError as e:
        raise AppError(ERR_BAD_INPUT, str(e), 400) from e
    if len(sig) != 64 or len(app_hash) != 32:
        raise AppError(ERR_INVALID_CHECKPOINT, "签名必须 64 字节、app_hash 必须 32 字节", 400)

    # 验签：对不含 signature 字段的规范字节签名
    signed = {k: v for k, v in cp.items() if k != "signature"}
    msg = crypto.checkpoint_sign_bytes(signed)
    if not crypto.verify_signature(from_hex(trusted_pubkey_hex, "trusted_pubkey"), sig, msg):
        raise AppError(ERR_INVALID_CHECKPOINT, "检查点签名验证失败（公钥/内容不匹配）", 400)

    height = cp["height"]
    ts = cp["time_nanos"]
    if not isinstance(height, int) or not isinstance(ts, int):
        raise AppError(ERR_BAD_INPUT, "height/time_nanos 必须为整数", 400)

    # 高度必须单调（旧检查点 / 重放拒绝）
    if height < last_trusted_height:
        raise AppError(
            ERR_STALE_CHECKPOINT,
            f"检查点高度 {height} 早于已信任高度 {last_trusted_height}",
            400,
        )

    # 信任期：检查点时间必须晚于上次信任时间，且对当前链时间仍新鲜
    if ts < last_trusted_time:
        raise AppError(
            ERR_STALE_CHECKPOINT,
            f"检查点时间 {ts} 早于已信任时间 {last_trusted_time}",
            400,
        )
    if now_nanos is not None and now_nanos - ts > trusting_period_nanos:
        raise AppError(
            ERR_STALE_CHECKPOINT,
            f"检查点时间 {ts} 已超出信任期（trusting_period={trusting_period_nanos}）",
            400,
        )
    return cp


def _decode_siblings(p: dict) -> list[bytes]:
    raw = p.get("siblings")
    if not isinstance(raw, list) or len(raw) != 256:
        raise AppError(ERR_BAD_PROOF_STRUCTURE, "siblings 必须是 256 个十六进制哈希", 400)
    out: list[bytes] = []
    for s in raw:
        try:
            b = from_hex(s, "sibling")
        except ValueError as e:
            raise AppError(ERR_BAD_PROOF_STRUCTURE, str(e), 400) from e
        if len(b) != 32:
            raise AppError(ERR_BAD_PROOF_STRUCTURE, "每个兄弟哈希必须 32 字节", 400)
        out.append(b)
    return out


def verify_membership(
    proof: dict,
    app_hash_hex: str,
    expected_key: bytes,
    expected_value: Optional[bytes] = None,
) -> bytes:
    """验证存在性证明；可要求值精确等于 expected_value。返回值。"""
    try:
        key = from_hex(proof.get("key", ""), "proof.key")
        value = from_hex(proof.get("value", ""), "proof.value")
    except ValueError as e:
        raise AppError(ERR_BAD_PROOF_STRUCTURE, str(e), 400) from e
    if key != expected_key:
        raise AppError(ERR_PROOF_KEY, "证明路径与协议期望的状态键不一致", 400)
    siblings = _decode_siblings(proof)
    root = from_hex(app_hash_hex, "app_hash")
    calc = SparseMerkleTree.verify(root, key, value, siblings)
    if calc != root:
        raise AppError(ERR_PROOF_VALUE, "存在性证明重算根与检查点 app_hash 不一致", 400)
    if expected_value is not None and value != expected_value:
        raise AppError(
            ERR_PROOF_VALUE,
            "证明值与协议期望值不一致（状态存在但内容不符）",
            400,
        )
    return value


def verify_non_membership(proof: dict, app_hash_hex: str, expected_key: bytes) -> None:
    """验证不存在性证明：叶值必须为空且重算根一致。"""
    try:
        key = from_hex(proof.get("key", ""), "proof.key")
    except ValueError as e:
        raise AppError(ERR_BAD_PROOF_STRUCTURE, str(e), 400) from e
    if key != expected_key:
        raise AppError(ERR_PROOF_KEY, "证明路径与协议期望的状态键不一致", 400)
    value = proof.get("value", "")
    if value not in ("", None):
        # 非成员证明里值必须为空
        v = from_hex(value, "proof.value")
        if v:
            raise AppError(ERR_PROOF_VALUE, "期望不存在性证明，但证明值非空", 400)
    siblings = _decode_siblings(proof)
    root = from_hex(app_hash_hex, "app_hash")
    calc = SparseMerkleTree.verify(root, key, b"", siblings)
    if calc != root:
        raise AppError(ERR_PROOF_VALUE, "不存在性证明重算根与检查点 app_hash 不一致", 400)
