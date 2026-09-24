"""JWT 签发工具 —— 仅用于测试夹具与本地 demo 发行方。

网关生产路径不依赖本模块；这里把各种算法（含“故意混淆”的攻击令牌）
造出来，方便验证安全行为。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64u_json(obj: Any) -> str:
    return b64u(
        json.dumps(obj, separators=(",", ":"), sort_keys=False).encode("utf-8")
    )


def _rsa_params(pub: rsa.RSAPublicKey) -> dict[str, str]:
    nums = pub.public_numbers()
    size = (nums.n.bit_length() + 7) // 8
    return {
        "n": b64u(nums.n.to_bytes(size, "big")),
        "e": b64u(nums.e.to_bytes((nums.e.bit_length() + 7) // 8, "big")),
    }


def _ec_params(pub: ec.EllipticCurvePublicKey) -> tuple[str, dict[str, str]]:
    curve = pub.curve
    name = {
        ec.SECP256R1: "P-256",
        ec.SECP384R1: "P-384",
        ec.SECP521R1: "P-521",
    }[type(curve)]
    size = (curve.key_size + 7) // 8
    nums = pub.public_numbers()
    return name, {
        "x": b64u(nums.x.to_bytes(size, "big")),
        "y": b64u(nums.y.to_bytes(size, "big")),
    }


def public_jwk(key: Any, kid: str, *, alg: str | None = None) -> dict[str, Any]:
    """把私钥/公钥导出成最小验签 JWK（仅公钥参数）。"""
    if isinstance(key, (rsa.RSAPrivateKey, rsa.RSAPublicKey)):
        pub = key.public_key() if hasattr(key, "public_key") else key
        jwk: dict[str, Any] = {"kty": "RSA", "kid": kid, "use": "sig"}
        jwk.update(_rsa_params(pub))
    elif isinstance(
        key, (ec.EllipticCurvePrivateKey, ec.EllipticCurvePublicKey)
    ):
        pub = key.public_key() if hasattr(key, "public_key") else key
        crv, params = _ec_params(pub)
        jwk = {"kty": "EC", "kid": kid, "use": "sig", "crv": crv}
        jwk.update(params)
    elif isinstance(
        key,
        (ed25519.Ed25519PrivateKey, ed25519.Ed25519PublicKey),
    ):
        pub = key.public_key() if hasattr(key, "public_key") else key
        raw = pub.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        jwk = {"kty": "OKP", "kid": kid, "use": "sig", "crv": "Ed25519",
               "x": b64u(raw)}
    else:
        raise TypeError(f"不支持的密钥类型: {type(key)!r}")
    if alg is not None:
        jwk["alg"] = alg
    return jwk


def jwks_doc(*jwks: dict[str, Any]) -> dict[str, Any]:
    return {"keys": list(jwks)}


def generate_key(alg: str) -> Any:
    if alg.startswith(("RS", "PS")):
        return rsa.generate_private_key(public_exponent=65537, key_size=2048)
    if alg == "ES256":
        return ec.generate_private_key(ec.SECP256R1())
    if alg == "ES384":
        return ec.generate_private_key(ec.SECP384R1())
    if alg == "ES512":
        return ec.generate_private_key(ec.SECP521R1())
    if alg == "EdDSA":
        return ed25519.Ed25519PrivateKey.generate()
    raise ValueError(f"非对称密钥生成不支持算法 {alg!r}")


_HASH = {
    "256": hashes.SHA256,
    "384": hashes.SHA384,
    "512": hashes.SHA512,
}


def _sign_asym(alg: str, key: Any, signing_input: bytes) -> bytes:
    if alg.startswith("RS"):
        return key.sign(signing_input, padding.PKCS1v15(), _HASH[alg[-3:]]())
    if alg.startswith("PS"):
        hash_cls = _HASH[alg[-3:]]
        return key.sign(
            signing_input,
            padding.PSS(
                mgf=padding.MGF1(hash_cls()),
                salt_length=hash_cls.digest_size,
            ),
            hash_cls(),
        )
    if alg.startswith("ES"):
        der = key.sign(signing_input, ec.ECDSA(_HASH[alg[-3:]]()))
        # DER -> R||S 定长拼接（JWS 格式）
        r_len = (key.curve.key_size + 7) // 8
        r, s = _der_to_rs(der)
        return r.to_bytes(r_len, "big") + s.to_bytes(r_len, "big")
    if alg == "EdDSA":
        return key.sign(signing_input)
    raise ValueError(f"非对称签名不支持算法 {alg!r}")


def _der_to_rs(der: bytes) -> tuple[int, int]:
    # 简单 DER 解析：SEQUENCE { INTEGER r, INTEGER s }
    assert der[0] == 0x30
    idx = 2 if der[1] & 0x80 == 0 else 2 + (der[1] & 0x7F)
    assert der[idx] == 0x02
    rlen = der[idx + 1]
    r = int.from_bytes(der[idx + 2: idx + 2 + rlen], "big")
    idx2 = idx + 2 + rlen
    assert der[idx2] == 0x02
    slen = der[idx2 + 1]
    s = int.from_bytes(der[idx2 + 2: idx2 + 2 + slen], "big")
    return r, s


def sign_jwt(
    claims: dict[str, Any],
    key: Any,
    *,
    alg: str,
    kid: str,
    extra_headers: dict[str, Any] | None = None,
) -> str:
    """用任意给定密钥（含 HMAC bytes）签发紧凑 JWT。"""
    header: dict[str, Any] = {"alg": alg, "typ": "JWT", "kid": kid}
    if extra_headers:
        header.update(extra_headers)
    signing_input = f"{b64u_json(header)}.{b64u_json(claims)}".encode("ascii")

    if alg.startswith("HS"):
        digest = {"HS256": hashlib.sha256,
                  "HS384": hashlib.sha384,
                  "HS512": hashlib.sha512}[alg]
        if isinstance(key, str):
            key = key.encode("utf-8")
        sig = hmac.new(key, signing_input, digest).digest()
    elif alg == "none":
        sig = b""
    else:
        sig = _sign_asym(alg, key, signing_input)
    return f"{signing_input.decode('ascii')}.{b64u(sig)}"


def random_hmac_secret(n: int = 48) -> bytes:
    return os.urandom(n)
