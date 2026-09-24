#!/usr/bin/env python3
"""本地 demo “发行方”服务（仅用于演示与手工验证，非网关组件）。

提供三个发行方的 JWKS 与现签令牌接口，监听 127.0.0.1，端口与网关
config/issuers.json 对应：

    GET  /rsa/jwks.json           RSA 发行方 JWKS（kid: rsa-1）
    GET  /ec/jwks.json            EC/EdDSA 发行方 JWKS（kid: ec-1, ed-1）
    POST /token                   {"issuer": "rsa|ec|hmac", "alg": "...",
                                   "aud": "...", "lifetime": 300,
                                   "nbf_delta": 0, "expired": false}
                                 -> {"token": "...", "kid": ...}
    POST /rotate                  生成全新密钥并替换 JWKS（模拟轮换）

向进程发送 SIGUSR1 也可触发轮换。
"""

from __future__ import annotations

import json
import signal
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from app.jwt_sign import (
    generate_key,
    jwks_doc,
    public_jwk,
    sign_jwt,
)

HOST = "127.0.0.1"
PORT = 8787

# 与 config/issuers.json 完全对应的 HMAC 密钥字节。
HMAC_SECRET = (
    b"demo-hmac-secret-please-change-32B+"
)


class IssuerState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.rsa_key = generate_key("RS256")
        self.ec_key = generate_key("ES256")
        self.ed_key = generate_key("EdDSA")
        self.generation = 1

    def rotate(self) -> None:
        with self.lock:
            self.rsa_key = generate_key("RS256")
            self.ec_key = generate_key("ES256")
            self.ed_key = generate_key("EdDSA")
            self.generation += 1


STATE = IssuerState()

ISSUER_META = {
    "rsa": {
        "iss": "https://demo-issuer.local/rsa",
        "default_aud": "api-gateway",
        "algs": {
            "RS256": ("rsa", "rsa-1"),
            "RS384": ("rsa", "rsa-1"),
            "RS512": ("rsa", "rsa-1"),
            "PS256": ("rsa", "rsa-1"),
        },
    },
    "ec": {
        "iss": "https://demo-issuer.local/ec",
        "default_aud": "api-gateway",
        "algs": {
            "ES256": ("ec", "ec-1"),
            "ES384": ("ec", "ec-1"),
            "ES512": ("ec", "ec-1"),
            "EdDSA": ("ed", "ed-1"),
        },
    },
    "hmac": {
        "iss": "https://demo-issuer.local/hmac",
        "default_aud": "internal-service",
        "algs": {"HS256": ("hmac", "hmac-1")},
    },
}


def _now() -> int:
    import time

    return int(time.time())


def _mint_token(body: dict) -> dict:
    issuer = body.get("issuer", "rsa")
    if issuer not in ISSUER_META:
        raise ValueError(f"未知 issuer {issuer!r}，可选 rsa/ec/hmac")
    meta = ISSUER_META[issuer]
    alg = body.get("alg") or next(iter(meta["algs"]))
    if alg not in meta["algs"]:
        raise ValueError(
            f"发行方 {issuer} 不支持 alg={alg!r}，可选 {sorted(meta['algs'])}"
        )
    kind, kid = meta["algs"][alg]

    now = _now()
    lifetime = int(body.get("lifetime", 300))
    if body.get("expired"):
        lifetime = -60
    claims = {
        "iss": meta["iss"],
        "sub": body.get("sub", "demo-user-42"),
        "aud": body.get("aud", meta["default_aud"]),
        "iat": now,
        "exp": now + lifetime,
        "jti": f"demo-{now}",
    }
    nbf_delta = body.get("nbf_delta")
    if nbf_delta is not None:
        claims["nbf"] = now + int(nbf_delta)

    with STATE.lock:
        if kind == "rsa":
            key = STATE.rsa_key
        elif kind == "ec":
            key = STATE.ec_key
        elif kind == "ed":
            key = STATE.ed_key
        else:
            key = HMAC_SECRET
    token = sign_jwt(claims, key, alg=alg, kid=kid)
    return {"token": token, "kid": kid, "alg": alg, "claims": claims}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, obj: dict | list) -> None:
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=5")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        with STATE.lock:
            gen = STATE.generation
            if self.path == "/rsa/jwks.json":
                doc = jwks_doc(public_jwk(STATE.rsa_key, "rsa-1", alg=None))
            elif self.path == "/ec/jwks.json":
                doc = jwks_doc(
                    public_jwk(STATE.ec_key, "ec-1", alg=None),
                    public_jwk(STATE.ed_key, "ed-1", alg=None),
                )
            else:
                self._send(404, {"error": "not_found", "path": self.path})
                return
        self.send_response(200)
        data = json.dumps(doc).encode("utf-8")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=5")
        self.send_header("X-Key-Generation", str(gen))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._send(400, {"error": "bad_json"})
            return
        if self.path == "/token":
            try:
                self._send(200, _mint_token(body))
            except ValueError as e:
                self._send(400, {"error": "bad_request", "detail": str(e)})
        elif self.path == "/rotate":
            STATE.rotate()
            self._send(200, {"rotated": True, "generation": STATE.generation})
        else:
            self._send(404, {"error": "not_found", "path": self.path})

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[issuer] {self.address_string()} - {fmt % args}")


def main() -> None:
    server = HTTPServer((HOST, PORT), Handler)

    def _rotate(signum: int, frame: object) -> None:
        STATE.rotate()
        print(f"[issuer] rotated keys (generation={STATE.generation})")

    signal.signal(signal.SIGUSR1, _rotate)
    print(f"[issuer] demo issuer listening on http://{HOST}:{PORT}")
    print("[issuer] endpoints: /rsa/jwks.json /ec/jwks.json /token /rotate")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
