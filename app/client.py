"""命令式客户端：封装 nonce 获取、Ed25519 签名信封与响应验签。

供自动化测试与 scripts/acceptance.sh 使用，真实执行全部密码操作。
"""
from __future__ import annotations

import time
from typing import Any

import httpx

from .crypto import (
    canonical,
    generate_keypair,
    key_id,
    public_pem,
    verify_object_signature,
)
from .store import canonical_envelope


class ClientError(RuntimeError):
    pass


class DrainClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8000", register: bool = True):
        self.http = httpx.Client(base_url=base_url, timeout=10, trust_env=False)
        priv, pub = generate_keypair()
        self.priv, self.pub = priv, pub
        self.kid = key_id(pub)
        self.server_pub = None
        self.server_kid = None
        if register:
            self.bootstrap()

    def bootstrap(self) -> None:
        info = self.http.get("/api/server-key").json()
        from .crypto import parse_public_pem

        self.server_pub = parse_public_pem(info["public_key_pem"])
        self.server_kid = info["kid"]
        r = self.http.post("/api/client-keys", json={"public_key_pem": public_pem(self.pub)})
        r.raise_for_status()

    def close(self) -> None:
        self.http.close()

    # ---------- 内部 ----------

    def _nonce(self) -> str:
        return self.http.post("/api/nonces").json()["nonce"]

    def _signed(self, payload: dict[str, Any]) -> dict[str, Any]:
        nonce, ts = self._nonce(), int(time.time())
        signable = canonical_envelope(self.kid, nonce, ts, payload)
        return {
            "kid": self.kid,
            "nonce": nonce,
            "ts": ts,
            "payload": payload,
            "sig": self.priv.sign(canonical(signable)).hex(),
        }

    def call(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = self._signed(payload or {})
        r = self.http.request(method, path, json=body)
        if r.status_code >= 400:
            raise ClientError(f"{method} {path} -> {r.status_code}: {r.text}")
        data = r.json()
        self._verify_response(data, path)
        return data["payload"]

    def _verify_response(self, data: dict[str, Any], path: str) -> None:
        if not isinstance(data, dict) or "signature" not in data:
            return  # 未签名的引导接口
        env = data["signature"]
        if env["kid"] != self.server_kid or self.server_pub is None:
            raise ClientError(f"{path}: 响应签名 kid 与服务器公钥不匹配")
        if not verify_object_signature(self.server_pub, data["payload"], env["sig"]):
            raise ClientError(f"{path}: 响应签名校验失败")

    # ---------- 语义化封装 ----------

    def load_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        return self.call("POST", "/api/snapshot", {"snapshot": snapshot})

    def plan(self, snapshot: dict[str, Any] | None, drain_nodes: list[str], force: bool = False) -> dict[str, Any]:
        return self.call(
            "POST", "/api/plans",
            {"snapshot": snapshot or {}, "drain_nodes": drain_nodes, "force": force},
        )

    def execute(self, plan_id: str, **opts: Any) -> dict[str, Any]:
        return self.call("POST", "/api/executions", {"plan_id": plan_id, **opts})

    def advance(self, exec_id: str) -> dict[str, Any]:
        return self.call("POST", f"/api/executions/{exec_id}/advance", {})

    def cancel(self, exec_id: str) -> dict[str, Any]:
        return self.call("POST", f"/api/executions/{exec_id}/cancel", {})

    def resume(self, exec_id: str) -> dict[str, Any]:
        return self.call("POST", f"/api/executions/{exec_id}/resume", {})

    def fault(self, namespace: str, deployment: str, mode: str, delay: int | None = None) -> dict[str, Any]:
        return self.call(
            "POST", "/api/sim/fault",
            {"namespace": namespace, "deployment": deployment, "mode": mode, "delay": delay},
        )

    def tick(self) -> dict[str, Any]:
        return self.call("POST", "/api/sim/tick", {})

    def get_execution(self, exec_id: str) -> dict[str, Any]:
        r = self.http.get(f"/api/executions/{exec_id}")
        r.raise_for_status()
        data = r.json()
        self._verify_response(data, f"GET {exec_id}")
        return data["payload"]

    def get_snapshot(self) -> dict[str, Any]:
        r = self.http.get("/api/snapshot")
        r.raise_for_status()
        data = r.json()
        self._verify_response(data, "GET snapshot")
        return data["payload"]

    def replay_raw(self, env: dict[str, Any], path: str) -> httpx.Response:
        """重放同一个签名信封（测试防重放）。"""
        return self.http.post(path, json=env)

    def build_envelope(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._signed(payload)
