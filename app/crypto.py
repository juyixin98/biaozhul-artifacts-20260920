"""密码学操作：Ed25519 事件签名与报告签名，真实执行（PyNaCl）。

签名内容定义：
- 事件信封 {"event_id", "ts", "type", "payload"}（字段固定、键排序、无空白）
- 报告摘要 canonical({"version","prev_hash","event_ids","created_from_ts","created_at"})

canonical JSON：UTF-8、键递归排序、紧凑分隔符、整数/字符串原样，
保证任何语言实现都能复现同样的签名字节。
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass

from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey

SIGNED_EVENT_FIELDS = ("event_id", "ts", "type", "payload")


def canonical(obj) -> bytes:
    """确定性 JSON 编码。"""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def event_signing_bytes(event: dict) -> bytes:
    return canonical({k: event[k] for k in SIGNED_EVENT_FIELDS})


def report_digest(report: dict) -> bytes:
    """报告哈希链摘要：对完整报告体（含 result）做 sha256，任何字段被篡改都会失败。"""
    return hashlib.sha256(canonical(report)).digest()


def digest_hex(report: dict) -> str:
    return report_digest(report).hex()


@dataclass(frozen=True)
class KeyPair:
    signing_key: SigningKey

    @property
    def private_hex(self) -> str:
        return bytes(self.signing_key).hex()

    @property
    def public_hex(self) -> str:
        return bytes(self.signing_key.verify_key).hex()

    def sign(self, message: bytes):
        """与 PyNaCl SigningKey.sign 一致，返回带 .signature 的 Signed。"""
        return self.signing_key.sign(message)

    def sign_message(self, message: bytes) -> bytes:
        """返回 64 字节原始 Ed25519 签名。"""
        return self.signing_key.sign(message).signature

    def sign_hex(self, message: bytes) -> str:
        return self.sign_message(message).hex()


def generate_keypair() -> KeyPair:
    return KeyPair(SigningKey.generate())


def load_signing_key(hex_or_env: str, path: str | None) -> SigningKey:
    """优先取显式 hex（通常来自环境变量），否则从文件读取。"""
    if hex_or_env:
        return SigningKey(bytes.fromhex(hex_or_env))
    if path and os.path.exists(path):
        return SigningKey(bytes.fromhex(open(path, "r", encoding="ascii").read().strip()))
    raise FileNotFoundError(f"找不到签名私钥：请设置环境变量或生成 {path}")


def load_verify_key(hex_or_env: str, path: str | None) -> VerifyKey:
    if hex_or_env:
        return VerifyKey(bytes.fromhex(hex_or_env))
    if path and os.path.exists(path):
        return VerifyKey(bytes.fromhex(open(path, "r", encoding="ascii").read().strip()))
    raise FileNotFoundError(f"找不到验签公钥：请设置环境变量或生成 {path}")


def verify_event(event: dict, key: VerifyKey) -> bool:
    """验证事件签名。返回 True/False，不抛异常（非法 hex 等一律视为无效）。"""
    sig_hex = event.get("sig")
    if not isinstance(sig_hex, str):
        return False
    try:
        key.verify(event_signing_bytes(event), bytes.fromhex(sig_hex))
        return True
    except (BadSignatureError, ValueError):
        return False
