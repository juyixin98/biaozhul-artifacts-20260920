"""全局状态与签名信封校验。"""
from __future__ import annotations

import secrets
import threading
import time
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .crypto import (
    SigningIdentity,
    generate_keypair,
    key_id,
    parse_public_pem,
    verify_object_signature,
)
from .executor import Executor
from .simulator import Simulator

NONCE_TTL_SECONDS = 300


class Store:
    def __init__(self) -> None:
        priv, pub = generate_keypair()
        self.server_identity = SigningIdentity(priv, pub)
        self.sim = Simulator()
        self.executor = Executor(self.sim)
        self.plans: dict[str, dict[str, Any]] = {}
        self.client_keys: dict[str, Ed25519PublicKey] = {}
        self.nonces: dict[str, float] = {}
        self.lock = threading.RLock()
        self.snapshot_loaded = False

    # ---------- nonce ----------

    def issue_nonce(self) -> dict[str, str]:
        nonce = secrets.token_urlsafe(24)
        with self.lock:
            self.nonces[nonce] = time.time() + NONCE_TTL_SECONDS
        return {
            "nonce": nonce,
            "expires_in": str(NONCE_TTL_SECONDS),
            "kid": self.server_identity.kid,
        }

    def _consume_nonce(self, nonce: str) -> None:
        now = time.time()
        with self.lock:
            # 顺带清理过期 nonce
            for n in [k for k, exp in self.nonces.items() if exp < now]:
                del self.nonces[n]
            exp = self.nonces.get(nonce)
            if exp is None:
                raise AuthError("nonce 无效或已被使用（防重放）")
            if exp < now:
                del self.nonces[nonce]
                raise AuthError("nonce 已过期")
            del self.nonces[nonce]  # 一次性

    # ---------- 客户端密钥 ----------

    def register_client_key(self, pem: str) -> dict[str, str]:
        pub = parse_public_pem(pem)
        kid = key_id(pub)
        with self.lock:
            self.client_keys[kid] = pub
        return {"kid": kid, "registered": "true"}

    # ---------- 信封 ----------

    def verify_envelope(self, env: dict[str, Any]) -> dict[str, Any]:
        """校验 {kid, nonce, ts, payload, sig}，返回解包后的 payload。"""
        try:
            kid = env["kid"]
            nonce = env["nonce"]
            ts = int(env["ts"])
            payload = env["payload"]
            sig_hex = env["sig"]
        except (KeyError, TypeError, ValueError) as e:
            raise AuthError(f"信封字段缺失或格式错误: {e}")
        with self.lock:
            pub = self.client_keys.get(kid)
        if pub is None:
            raise AuthError(f"未知客户端 kid {kid}，请先 /api/client-keys 注册公钥")
        if abs(time.time() - ts) > NONCE_TTL_SECONDS:
            raise AuthError("时间戳超出允许窗口（±300s）")
        signable = canonical_envelope(kid, nonce, ts, payload)
        if not verify_object_signature(pub, signable, sig_hex):
            raise AuthError("签名校验失败：payload/nonce/ts 可能被篡改")
        self._consume_nonce(nonce)
        return payload

    def sign_response_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"payload": payload, "signature": self.server_identity.sign_object(payload)}


def canonical_envelope(kid: str, nonce: str, ts: int, payload: Any) -> dict[str, Any]:
    return {"kid": kid, "nonce": nonce, "ts": ts, "payload": payload}


class AuthError(Exception):
    pass
