"""真实执行的密码学操作（标准库 hashlib / hmac，无占位实现）。

* request_sha256 —— 对**原始请求字节**计算 SHA-256，作为请求指纹回传，
  调用方可以自行复算，确认服务处理的确实是自己发出的那份字节。
* sign_response —— 若设置了环境变量 GROUNDSEG_HMAC_KEY，则对规范化的
  响应 JSON（sort_keys、分隔符固定、UTF-8）计算 HMAC-SHA256，
  供接收方验证响应在传输中未被篡改。未配置密钥时不签名。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os

HMAC_ALGO = "HMAC-SHA256"
SHA_ALGO = "SHA-256"


def request_sha256(raw_body: bytes) -> str:
    """对原始 HTTP 请求体计算 SHA-256 十六进制摘要（真实计算）。"""
    if not isinstance(raw_body, (bytes, bytearray)):
        raise TypeError("raw_body 必须是 bytes")
    return hashlib.sha256(raw_body).hexdigest()


def canonical_json(obj) -> bytes:
    """确定性 JSON 序列化：sort_keys + 紧凑分隔符，保证签名可复现。"""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def response_sha256(obj) -> str:
    """对规范化响应体计算 SHA-256。"""
    return hashlib.sha256(canonical_json(obj)).hexdigest()


def get_hmac_key() -> bytes | None:
    key = os.environ.get("GROUNDSEG_HMAC_KEY")
    if key:
        return key.encode("utf-8")
    return None


def sign_response(obj) -> dict[str, str] | None:
    """对响应对象做 HMAC-SHA256。未配置密钥返回 None。

    返回 {"algorithm", "signed_fields", "mac"}。签名覆盖除 signature
    字段外的整个响应体。
    """
    key = get_hmac_key()
    if key is None:
        return None
    payload = obj
    if isinstance(obj, dict) and "signature" in obj:
        payload = {k: v for k, v in obj.items() if k != "signature"}
    mac = hmac.new(key, canonical_json(payload), hashlib.sha256).hexdigest()
    return {
        "algorithm": HMAC_ALGO,
        "signed_fields": "all_except_signature",
        "mac": mac,
    }


def verify_mac(obj, mac: str, key: bytes) -> bool:
    """校验工具（测试与外部核对都可用），常量时间比较。"""
    expected = hmac.new(key, canonical_json(obj), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, mac)
