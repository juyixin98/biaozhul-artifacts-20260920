"""本地演示/测试用密钥与签名构造辅助。

生产环境中 feeder 私钥由各 feeder 自持；此模块仅用于生成本地密钥对、
构造规范事件载荷并用私钥真实签名，方便跑通端到端流程。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .crypto import generate_private_key, public_key_hex, sign_payload


class DemoFeeder:
    def __init__(self, label: str = "demo-feeder", seed: bytes | None = None):
        self.label = label
        if seed is None:
            self.private_key = generate_private_key()
        else:
            if len(seed) != 32:
                raise ValueError("Ed25519 seed must be 32 bytes")
            self.private_key = Ed25519PrivateKey.from_private_bytes(seed)

    @property
    def public_key(self) -> str:
        return public_key_hex(self.private_key)

    def event(
        self,
        *,
        chain_id: str,
        event_type: str,
        tx_hash: str,
        log_index: int,
        block_height: int,
        block_hash: str,
        source_chain: str,
        original_contract: str,
        source_nonce: str,
        account: str,
        amount: int | str,
        token_id: str = "",
    ) -> dict[str, Any]:
        payload = {
            "chain_id": chain_id,
            "event_type": event_type,
            "tx_hash": tx_hash,
            "log_index": log_index,
            "block_height": block_height,
            "block_hash": block_hash,
            "source_chain": source_chain,
            "original_contract": original_contract,
            "token_id": token_id,
            "source_nonce": source_nonce,
            "account": account,
            "amount": str(amount),
        }
        signature = sign_payload(self.private_key, payload)
        return {"payload": payload, "signature_hex": signature,
                "feeder_public_key_hex": self.public_key}


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()
