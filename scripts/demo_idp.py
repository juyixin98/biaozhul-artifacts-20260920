#!/usr/bin/env python3
"""Tiny demo/training identity provider.

Exposes JWKS endpoints for two issuers (RS256 and ES256), mints tokens for
the happy path and a set of deliberately hostile variants, and supports key
rotation.  It is NOT part of the gateway and uses only the shared JWS
helpers + Python's stdlib HTTP server.

Endpoints
---------
GET  /rsa/jwks.json, /ec/jwks.json      current public key sets
GET  /dup/jwks.json                     two RS256 keys sharing one kid
GET  /rsa/jwks-rotated.json etc.        not used; rotation is in-place
POST /mint                               {"mode": ..., "claims": {...}}
GET  /rotate?issuer=rsa|ec               generate a fresh key + kid
POST /rotate                             body {"issuer": "rsa|ec"}
GET  /healthz

Mint modes: valid | none | hmac-confuse | jku | attacker-rsa | unknown-kid
| expired | nbf-future | bad-aud | bad-iss
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from app.jwtv.b64 import b64url_encode
from app.jwtv.jwk import public_jwk_from_private_key
from app.jwtv.jws import sign

RSA_ISS = "https://demo-idp.local/rsa"
EC_ISS = "https://demo-idp.local/ec"
DUP_ISS = "https://demo-idp.local/dup"
AUDIENCE = "gateway-demo"


class KeyState:
    def __init__(self, name: str, alg: str, curve: str | None = None) -> None:
        self.name = name
        self.alg = alg
        self.lock = threading.Lock()
        self.generation = 0
        self.rotate(curve, initial=True)

    def rotate(self, curve: str | None = None, initial: bool = False) -> None:
        with self.lock:
            self.generation += 1
            if self.alg.startswith("RS"):
                self.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            else:
                self.private = ec.generate_private_key(ec.SECP256R1())
            self.kid = f"{self.name}-k{self.generation}"

    @property
    def public_jwk(self) -> dict[str, Any]:
        with self.lock:
            return public_jwk_from_private_key(self.private, self.kid, self.alg)


STATE: dict[str, KeyState] = {}


def _dup_jwks() -> dict[str, Any]:
    # Two distinct RSA keys advertising the SAME kid -> ambiguous key set.
    k1 = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    k2 = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk1 = public_jwk_from_private_key(k1, "dup-kid", "RS256")
    jwk2 = public_jwk_from_private_key(k2, "dup-kid", "RS256")
    return {"keys": [jwk1, jwk2]}


DUP_DOCUMENT = _dup_jwks()


def _encode_segment(data: bytes) -> str:
    return b64url_encode(data)


def mint(spec: dict[str, Any]) -> str:
    mode = spec.get("mode", "valid")
    issuer_name = spec.get("issuer", "rsa")
    overrides = spec.get("claims") or {}
    now = int(time.time())

    state = STATE[issuer_name]
    iss_value = {
        "rsa": RSA_ISS,
        "ec": EC_ISS,
    }.get(issuer_name, spec.get("iss", RSA_ISS))
    alg = spec.get("alg") or state.alg
    kid = spec.get("kid") or state.kid

    claims: dict[str, Any] = {
        "iss": iss_value,
        "sub": "demo-user-42",
        "aud": AUDIENCE,
        "iat": now,
        "nbf": now - 10,
        "exp": now + 3600,
        "jti": f"demo-{now}",
    }
    claims.update(overrides)

    header: dict[str, Any] = {"alg": alg, "typ": "JWT", "kid": kid}

    if mode == "none":
        header["alg"] = "none"
        header.pop("kid", None)
        h = _encode_segment(json.dumps(header, separators=(",", ":")).encode())
        p = _encode_segment(json.dumps(claims, separators=(",", ":")).encode())
        return f"{h}.{p}."

    if mode == "hmac-confuse":
        # Classic RS256->HS256 confusion: sign with the RSA *public* key
        # bytes treated as an HMAC secret, reusing the real key's kid.
        header["alg"] = "HS256"
        public_der = state.private.public_key().public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        h = _encode_segment(json.dumps(header, separators=(",", ":")).encode())
        p = _encode_segment(json.dumps(claims, separators=(",", ":")).encode())
        sig = sign("HS256", public_der, f"{h}.{p}".encode())
        return f"{h}.{p}.{_encode_segment(sig)}"

    if mode == "jku":
        header["jku"] = "https://attacker.example/evil-jwks.json"

    if mode in ("attacker-rsa", "unknown-kid"):
        forged = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        signer = forged
        header["alg"] = "RS256"
        header["kid"] = state.kid if mode == "attacker-rsa" else "kid-that-does-not-exist"
    else:
        signer = state.private

    if mode == "expired":
        claims["exp"] = now - 60
        claims["nbf"] = now - 3600
    if mode == "nbf-future":
        claims["nbf"] = now + 3600
        claims["exp"] = now + 7200
    if mode == "bad-aud":
        claims["aud"] = "someone-else"
    if mode == "bad-iss":
        claims["iss"] = "https://attacker.example/rsa"

    h = _encode_segment(json.dumps(header, separators=(",", ":")).encode())
    p = _encode_segment(json.dumps(claims, separators=(",", ":")).encode())
    sig = sign(header["alg"], signer, f"{h}.{p}".encode())
    return f"{h}.{p}.{_encode_segment(sig)}"


class Handler(BaseHTTPRequestHandler):
    server_version = "demo-idp/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[idp] {self.address_string()} {fmt % args}", flush=True)

    def _send_json(self, status: int, body: Any) -> None:
        data = json.dumps(body, indent=2).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/healthz":
            self._send_json(200, {"status": "ok"})
        elif path in ("/rsa/jwks.json", "/ec/jwks.json"):
            name = path.split("/", 2)[1]
            self._send_json(200, {"keys": [STATE[name].public_jwk]})
        elif path == "/dup/jwks.json":
            self._send_json(200, DUP_DOCUMENT)
        elif path == "/rotate":
            name = parse_qs(parsed.query).get("issuer", ["rsa"])[0]
            self._rotate(name)
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            spec = json.loads(raw)
        except json.JSONDecodeError:
            self._send_json(400, {"error": "body must be JSON"})
            return
        if self.path == "/mint":
            try:
                token = mint(spec)
            except KeyError as exc:
                self._send_json(400, {"error": f"unknown issuer {exc}"})
                return
            self._send_json(200, {"token": token})
        elif self.path == "/rotate":
            self._rotate(spec.get("issuer", "rsa"))
        else:
            self._send_json(404, {"error": "not found"})

    def _rotate(self, name: str) -> None:
        if name not in STATE:
            self._send_json(404, {"error": f"unknown issuer {name}"})
            return
        STATE[name].rotate()
        self._send_json(
            200,
            {"issuer": name, "kid": STATE[name].kid, "generation": STATE[name].generation},
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args()

    STATE["rsa"] = KeyState("rsa", "RS256")
    STATE["ec"] = KeyState("ec", "ES256")

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[idp] demo IdP listening on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
