"""元数据信封格式、规范化序列化与哈希。

元数据文件是一个信封::

    {
      "signatures": [{"keyid": "...", "sig": "<hex ed25519>"}],
      "signed": { ...角色专属内容... }
    }

所有签名和哈希都针对 ``signed`` 的规范化 JSON：
键按字典序排序、无空白、``ensure_ascii=False``、换行符结尾。
发送方与验证方必须使用同一规范，否则哈希绑定失效。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from . import crypto_utils
from .errors import BadFormatError, ExpiredError

ROLES = ("root", "targets", "snapshot", "timestamp")

CANONICAL_SUFFIX = b"\n"


def canonical(obj: dict | list) -> bytes:
    return (json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            + CANONICAL_SUFFIX)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha512_hex(data: bytes) -> str:
    return hashlib.sha512(data).hexdigest()


def file_hashes(data: bytes) -> dict:
    return {"sha256": sha256_hex(data), "sha512": sha512_hex(data)}


def parse_envelope(raw: bytes, *, role: str) -> dict:
    """解析并基本校验一个元数据信封，返回解析后的 JSON 对象。"""
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BadFormatError("BAD_JSON", f"{role} 元数据不是合法 JSON", {"role": role}) from exc
    if not isinstance(envelope, dict) or not isinstance(envelope.get("signed"), dict):
        raise BadFormatError("BAD_ENVELOPE", f"{role} 信封缺少 signed 对象", {"role": role})
    if not isinstance(envelope.get("signatures"), list):
        raise BadFormatError("BAD_ENVELOPE", f"{role} 信封缺少 signatures 列表", {"role": role})
    signed = envelope["signed"]
    expected_type = role
    if signed.get("_type") != expected_type:
        raise BadFormatError(
            "BAD_TYPE",
            f"{role} 元数据 _type 应为 {expected_type!r}，实际为 {signed.get('_type')!r}",
            {"role": role, "got": signed.get("_type")},
        )
    version = signed.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise BadFormatError("BAD_VERSION", f"{role} 版本号必须是 >=1 的整数", {"role": role})
    return envelope


def signed_bytes(envelope: dict) -> bytes:
    return canonical(envelope["signed"])


def build_signed(role: str, version: int, expires: str, body: dict) -> dict:
    """构造 signed 对象（调用方再用 :func:`sign_signed` 装信封）。"""
    body = dict(body)
    body["_type"] = role
    body["version"] = version
    body["expires"] = expires
    return body


def wrap_signed(signed: dict, signatures: list[dict]) -> dict:
    return {"signatures": signatures, "signed": signed}


def sign_signed(signed: dict, priv_hexes: list[str]) -> dict:
    data = canonical(signed)
    signatures = [
        {"keyid": crypto_utils.keyid_for(crypto_utils.public_from_private(p)), "sig": crypto_utils.sign(p, data)}
        for p in priv_hexes
    ]
    return wrap_signed(signed, signatures)


def iso_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_expires(value: str, *, role: str) -> datetime:
    if not isinstance(value, str):
        raise BadFormatError("BAD_EXPIRES", f"{role} 的 expires 必须是字符串", {"role": role})
    text = value.strip()
    try:
        if text.endswith("Z"):
            dt = datetime.fromisoformat(text[:-1]).replace(tzinfo=timezone.utc)
        else:
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt = dt.astimezone(timezone.utc)
    except ValueError as exc:
        raise BadFormatError("BAD_EXPIRES", f"{role} 的 expires 无法解析: {value!r}", {"role": role}) from exc
    return dt


def check_not_expired(expires: str, *, role: str, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    dt = parse_expires(expires, role=role)
    if dt <= now:
        raise ExpiredError(
            "EXPIRED",
            f"{role} 元数据已于 {expires} 过期",
            {"role": role, "expires": expires, "now": iso_utc(now)},
        )
