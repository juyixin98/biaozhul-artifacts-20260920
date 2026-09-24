"""密码学操作：用于"撤销预订"的防篡改令牌。

每个预约在提交时由服务器签发一个 cancellation token：

    base64url( payload_json )  +  "."  +  base64url( HMAC-SHA256(base, secret) )

payload 至少包含 ``resv_id`` 与一次性随机数 ``nonce``。撤销时服务端：

1. 拆分并 **重新计算** HMAC，用 :func:`hmac.compare_digest` 做常量时间比较；
2. 校验 payload 中的 ``resv_id`` 与路径参数一致。

这样客户端无法伪造或篡改别人的撤销令牌（例如把自己的 token 改写成别的预约 id）。
密钥在服务首次启动时随机生成并持久化进 SQLite（``metadata`` 表），重启后保持稳定，
从而旧令牌在重启后仍然有效。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets as _secrets
from typing import Any, Dict


def generate_secret() -> str:
    """生成 32 字节随机密钥，返回十六进制字符串（持久化用）。"""
    return _secrets.token_hex(32)


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(txt: str) -> bytes:
    pad = "=" * (-len(txt) % 4)
    return base64.urlsafe_b64decode(txt + pad)


def _sign(base: str, secret_hex: str) -> str:
    mac = hmac.new(bytes.fromhex(secret_hex), base.encode("utf-8"), hashlib.sha256)
    return _b64e(mac.digest())


def issue_token(secret_hex: str, payload: Dict[str, Any]) -> str:
    """对 payload 签发 token。内部会补一个一次性 nonce。"""
    body = dict(payload)
    body.setdefault("nonce", _secrets.token_hex(16))
    base = _b64e(json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return base + "." + _sign(base, secret_hex)


def verify_token(secret_hex: str, token: str) -> Dict[str, Any]:
    """验证 token，返回其中的 payload。

    任何格式错误、签名不符都抛 :class:`TokenError`。
    """
    try:
        base, sig = token.split(".", 1)
    except (ValueError, AttributeError):
        raise TokenError("令牌格式错误")
    expected = _sign(base, secret_hex)
    if not hmac.compare_digest(expected, sig):
        raise TokenError("令牌签名校验失败")
    try:
        payload = json.loads(_b64d(base))
    except Exception:
        raise TokenError("令牌载荷无法解析")
    if not isinstance(payload, dict) or "resv_id" not in payload:
        raise TokenError("令牌载荷缺少 resv_id")
    return payload


class TokenError(Exception):
    """撤销令牌无效或被篡改。"""
