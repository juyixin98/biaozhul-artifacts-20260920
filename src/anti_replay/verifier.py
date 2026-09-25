"""验证流水线：认证头 → 密钥查找 → 规范编码 → HMAC → 时间窗 → nonce 原子登记。

前 5 步任意失败都不会触碰 nonce 登记表（不把攻击者的垃圾输入写进表）；
只有完全合法的请求才会走到原子 ``claim``，从而保证“并发重复请求只接受一个”。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Mapping

from .canonical import CanonicalizationError, canonical_request_target
from .nonce_store import NonceStore
from .signing import verify_request_signature

KEY_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
TIMESTAMP_RE = re.compile(r"[0-9]{1,15}\Z")
NONCE_RE = re.compile(r"[A-Za-z0-9_-]{8,128}\Z")
SIGNATURE_RE = re.compile(r"[0-9a-f]{64}\Z")

DEFAULT_WINDOW_SECONDS = 300

# 错误码与 README 的错误表一一对应。
ERR_MALFORMED = "malformed_request"
ERR_UNKNOWN_KEY = "unknown_key"
ERR_BAD_SIGNATURE = "bad_signature"
ERR_STALE = "stale_timestamp"
ERR_FUTURE = "future_timestamp"
ERR_REPLAY = "replay_detected"


@dataclass(frozen=True)
class VerifyResult:
    accepted: bool
    error_code: str | None = None
    error_message: str | None = None
    key_id: str | None = None
    canonical_path: str | None = None


@dataclass(frozen=True)
class AuthResult:
    """``authenticate`` 成功的产物：已通过全部检查、但尚未登记 nonce。

    调用方应在完成所有可能失败的请求处理前置工作（如正文解析、业务校验）
    **之后**，再调用 :meth:`Verifier.commit_nonce` 原子登记——这样任何在
    “生效”前发生的失败都不会烧掉 nonce，诚实客户端可以安全重试。
    """

    key_id: str
    nonce: str
    timestamp: str
    ts: int
    canonical_path: str


def _normalize_headers(headers: Mapping[str, str] | object) -> dict[str, str]:
    """把 dict 或 HTTPMessage 统一成小写键映射（值去首尾空白）。"""
    items = headers.items()  # type: ignore[attr-defined]
    result: dict[str, str] = {}
    for name, value in items:
        result[name.lower()] = value.strip() if isinstance(value, str) else value
    return result


class Verifier:
    """无状态（除 nonce 存储与密钥表外）的请求验证器。"""

    def __init__(
        self,
        keys: Mapping[str, bytes],
        nonce_store: NonceStore,
        *,
        window_seconds: int = DEFAULT_WINDOW_SECONDS,
        clock=time.time,
    ) -> None:
        self._keys = dict(keys)
        self._nonces = nonce_store
        self._window = int(window_seconds)
        self._clock = clock

    def authenticate(
        self,
        *,
        method: str,
        headers: Mapping[str, str] | object,
        body: bytes,
        target: str | None = None,
        now: float | None = None,
        precomputed_target: tuple[str, str] | None = None,
    ) -> AuthResult | VerifyResult:
        """执行除 nonce 登记外的全部检查（不触碰 nonce 存储）。

        成功返回 :class:`AuthResult`；失败返回带错误码的 :class:`VerifyResult`
        （此时 ``accepted is False``）。

        ``target`` 为原始 request-target；若调用方（如 HTTP 服务，需要先按
        规范路径做路由）已经做过规范化，可通过 ``precomputed_target`` 传入
        ``(canonical_path, canonical_query)`` 直接复用。两者必须提供其一。
        """
        if target is None and precomputed_target is None:
            raise ValueError("either target or precomputed_target is required")
        if now is None:
            now = self._clock()
        h = _normalize_headers(headers)

        # ---- 1. 认证头存在性与格式 -------------------------------------
        key_id = h.get("x-auth-key-id", "")
        timestamp = h.get("x-auth-timestamp", "")
        nonce = h.get("x-auth-nonce", "")
        signature = h.get("x-auth-signature", "")
        if not key_id or not timestamp or not nonce or not signature:
            return VerifyResult(False, ERR_MALFORMED, "missing authentication header(s)")
        if not KEY_ID_RE.fullmatch(key_id):
            return VerifyResult(False, ERR_MALFORMED, "invalid key id format")
        if not TIMESTAMP_RE.fullmatch(timestamp):
            return VerifyResult(False, ERR_MALFORMED, "invalid timestamp format")
        if not NONCE_RE.fullmatch(nonce):
            return VerifyResult(False, ERR_MALFORMED, "invalid nonce format")
        if not SIGNATURE_RE.fullmatch(signature):
            return VerifyResult(False, ERR_MALFORMED, "invalid signature format")
        ts = int(timestamp)

        # ---- 2. 密钥查找 ------------------------------------------------
        key = self._keys.get(key_id)
        if key is None:
            return VerifyResult(False, ERR_UNKNOWN_KEY, "unknown key id", key_id=key_id)

        # ---- 3. 请求目标规范化 ------------------------------------------
        if precomputed_target is not None:
            cpath, cquery = precomputed_target
        else:
            assert target is not None
            try:
                cpath, cquery = canonical_request_target(target)
            except CanonicalizationError as exc:
                return VerifyResult(
                    False, ERR_MALFORMED, f"request target rejected: {exc}",
                    key_id=key_id,
                )

        # ---- 4. HMAC 签名（恒定时间） -----------------------------------
        if not verify_request_signature(
            key=key,
            key_id=key_id,
            method=method,
            canonical_path_value=cpath,
            canonical_query_value=cquery,
            body=body,
            timestamp=timestamp,
            nonce=nonce,
            signature_hex=signature,
        ):
            return VerifyResult(
                False, ERR_BAD_SIGNATURE, "signature verification failed", key_id=key_id
            )

        # ---- 5. 时间窗口（含边界） --------------------------------------
        delta = now - ts
        if delta > self._window:
            return VerifyResult(
                False, ERR_STALE,
                f"timestamp {abs(delta):.0f}s older than window {self._window}s",
                key_id=key_id,
            )
        if -delta > self._window:
            return VerifyResult(
                False, ERR_FUTURE,
                f"timestamp {abs(delta):.0f}s ahead of window {self._window}s",
                key_id=key_id,
            )

        return AuthResult(
            key_id=key_id, nonce=nonce, timestamp=timestamp,
            ts=ts, canonical_path=cpath,
        )

    def commit_nonce(
        self, auth: AuthResult, *, now: float | None = None
    ) -> VerifyResult | None:
        """原子登记 nonce —— 必须在请求“生效”前的最后一步调用。

        成功（首次登记）返回 ``None``；重放返回 ``replay_detected`` 结果。
        """
        if now is None:
            now = self._clock()
        if not self._nonces.claim(auth.key_id, auth.nonce, auth.ts, now):
            return VerifyResult(
                False, ERR_REPLAY, "nonce has already been used",
                key_id=auth.key_id,
            )
        return None

    def verify(
        self,
        *,
        method: str,
        headers: Mapping[str, str] | object,
        body: bytes,
        target: str | None = None,
        now: float | None = None,
        precomputed_target: tuple[str, str] | None = None,
    ) -> VerifyResult:
        """便捷封装：authenticate + commit_nonce 一气呵成。

        适用于“通过即生效、通过后不再有可失败步骤”的调用方与单元测试。
        有额外正文/业务校验的调用方（HTTP 服务）应分别调用
        :meth:`authenticate` 与 :meth:`commit_nonce`，以免失败烧掉 nonce。
        """
        if now is None:
            now = self._clock()
        auth = self.authenticate(
            method=method, headers=headers, body=body, target=target,
            now=now, precomputed_target=precomputed_target,
        )
        if isinstance(auth, VerifyResult):
            return auth
        replay = self.commit_nonce(auth, now=now)
        if replay is not None:
            return replay
        return VerifyResult(True, key_id=auth.key_id, canonical_path=auth.canonical_path)
