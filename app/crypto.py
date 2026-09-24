"""密码操作：HMAC-SHA256 请求签名 / 回执签名，全部基于标准库真实计算。"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from . import config


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sign_text(secret: str, message: str) -> str:
    """对文本计算 HMAC-SHA256，返回十六进制摘要。"""
    return hmac.new(
        secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_signature(secret: str, message: str, signature_hex: str) -> bool:
    """常量时间比较，防止时序侧信道。"""
    expected = sign_text(secret, message)
    return hmac.compare_digest(expected, signature_hex or "")


def canonical_json(obj: Any) -> str:
    """排序键、无空白的确定性 JSON，用于回执签名。"""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def signing_string(
    key_id: str,
    timestamp: str,
    nonce: str,
    method: str,
    path: str,
    body: bytes,
) -> str:
    """构造请求签名的待签名串（见 README「协议」一节）。"""
    return "\n".join(
        [
            key_id,
            timestamp,
            nonce,
            method.upper(),
            path,
            sha256_hex(body or b""),
        ]
    )


def sign_request(
    method: str,
    path: str,
    body: bytes,
    *,
    key_id: str | None = None,
    secret: str | None = None,
    timestamp: str | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    """客户端辅助：为一次 HTTP 请求生成签名头。"""
    import time
    import uuid

    key_id = key_id or config.API_KEY_ID
    secret = secret or config.API_SECRET
    timestamp = timestamp or str(int(time.time()))
    nonce = nonce or uuid.uuid4().hex
    message = signing_string(key_id, timestamp, nonce, method, path, body)
    return {
        "X-Key-Id": key_id,
        "X-Timestamp": timestamp,
        "X-Nonce": nonce,
        "X-Signature": sign_text(secret, message),
    }


def receipt_payload(
    *,
    allocation_id: int,
    task_id: str,
    robot_id: str,
    charger_id: str | None,
    outbound_energy_wh: float,
    wait_energy_wh: float,
    return_energy_wh: float,
    total_energy_wh: float,
    safety_margin_wh: float,
    created_at: str,
) -> dict[str, str]:
    """对一条分配结果签发可验真的回执（签名覆盖所有关键结算字段）。"""
    fields = {
        "allocation_id": allocation_id,
        "task_id": task_id,
        "robot_id": robot_id,
        "charger_id": charger_id,
        "outbound_energy_wh": round(outbound_energy_wh, 6),
        "wait_energy_wh": round(wait_energy_wh, 6),
        "return_energy_wh": round(return_energy_wh, 6),
        "total_energy_wh": round(total_energy_wh, 6),
        "safety_margin_wh": round(safety_margin_wh, 6),
        "created_at": created_at,
    }
    canonical = canonical_json(fields)
    return {
        "fields": canonical,
        "algorithm": "HMAC-SHA256",
        "key_id": config.API_KEY_ID,
        "signature": sign_text(config.API_SECRET, canonical),
    }


def verify_receipt(fields_canonical: str, signature_hex: str) -> bool:
    return verify_signature(config.API_SECRET, fields_canonical, signature_hex)
