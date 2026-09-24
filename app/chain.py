"""哈希链与检查点结构定义。

记录（entry）：
    {seq, ts, actor, action, payload, prev_hash}
    entry_hash = sha256(canonical_json(entry))
    创世记录的 prev_hash 为 64 个 0。

检查点（checkpoint）：
    {log_id, upto_seq, head_hash, key_id, created_at}
    signature = Ed25519_sign(private_key, canonical_json(checkpoint))
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from .canonical import hash_object

GENESIS_PREV_HASH = "sha256:" + "0" * 64


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def build_entry(
    seq: int,
    prev_hash: str,
    actor: str,
    action: str,
    payload: Any,
    ts: Optional[str] = None,
) -> tuple[dict, str]:
    """构建一条记录并返回 (entry, entry_hash)。"""
    entry = {
        "seq": seq,
        "ts": ts or now_iso(),
        "actor": actor,
        "action": action,
        "payload": payload,
        "prev_hash": prev_hash,
    }
    return entry, hash_object(entry)


def build_checkpoint(
    log_id: str,
    upto_seq: int,
    head_hash: str,
    key_id: str,
    created_at: Optional[str] = None,
) -> dict:
    """构建待签名的检查点对象（签名本身不包含在内）。"""
    return {
        "log_id": log_id,
        "upto_seq": upto_seq,
        "head_hash": head_hash,
        "key_id": key_id,
        "created_at": created_at or now_iso(),
    }
