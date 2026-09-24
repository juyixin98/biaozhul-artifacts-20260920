"""JWT 验证核心。

验证顺序（先结构、后密码学、再声明），任何一步失败立即返回结构化错误：

1. 紧凑结构（恰好 3 段），拒绝带 ``zip`` 的嵌套/压缩 JWT；
2. base64url + 严格 JSON（拒绝重复键），头/体字段类型逐一检查；
3. 头中 ``alg`` 必须在**该发行方**的显式白名单中（``none`` 与任何未列入的
   算法直接拒绝，因此 RS->HS 算法混淆在配置层面就不可能通过）；
4. 头中 ``jku`` / ``x5u`` / ``jwk`` / ``x5c`` 等可指定取钥地址/内嵌公钥的
   参数一律拒绝，``crit`` 列出的关键扩展无法识别从而拒绝；
5. 凭 ``kid`` 从**该发行方隔离缓存**取钥（对称密钥来自服务端配置）；
6. 用密码学库实际验签，绝不只做字符串比较；
7. 校验 ``iss``（须与配置完全一致）、``exp``（必填）、``nbf``（选填）、
   ``aud``（必填且须命中期望集合），时间比较支持配置的 leeway。
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any, Callable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from .config import SYM_ALGORITHMS, IssuerConfig, TrustStore
from .errors import ErrCode, VerifyError
from .jwks import (
    _ALG_CURVE,
    _ALG_KTY,
    _EC_CURVES,
    JwksCacheRegistry,
    b64url_decode,
    ec_signature_to_dss,
    parse_json_strict,
)

# 可被令牌用来“告诉验证方去哪取钥/用什么钥”的头参数 —— 本服务一律不认。
_FORBIDDEN_HEADER_PARAMS = frozenset(
    {"jku", "jwk", "x5u", "x5c", "x5t", "x5t#S256"}
)

_HASH_FACTORIES: dict[str, type[hashes.HashAlgorithm]] = {
    "256": hashes.SHA256,
    "384": hashes.SHA384,
    "512": hashes.SHA512,
}
_HMAC_DIGEST = {
    "HS256": hashlib.sha256,
    "HS384": hashlib.sha384,
    "HS512": hashlib.sha512,
}
_HMAC_MIN_KEY_BYTES = 32


def _hash_factory(alg: str) -> type[hashes.HashAlgorithm]:
    try:
        return _HASH_FACTORIES[alg[-3:]]
    except KeyError:
        raise VerifyError(
            ErrCode.ALG_NOT_ALLOWED, f"未知算法族: {alg!r}"
        ) from None


def _require_str_field(
    obj: dict[str, Any], key: str, *, what: str, in_header: bool
) -> str:
    v = obj.get(key)
    if not isinstance(v, str) or not v:
        raise VerifyError(
            ErrCode.BAD_HEADER_FIELD if in_header else ErrCode.BAD_CLAIM_TYPE,
            f"{what}的 {key!r} 必须是非空字符串",
            context={"field": key},
        )
    return v


def _numeric_timestamp(
    claims: dict[str, Any], key: str, *, required: bool
) -> int | float | None:
    if key not in claims:
        if required:
            raise VerifyError(
                ErrCode.MISSING_PAYLOAD_CLAIM,
                f"JWT 载荷缺少必填声明 {key!r}",
                context={"field": key},
            )
        return None
    v = claims[key]
    # bool 是 int 的子类，必须显式排除。
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise VerifyError(
            ErrCode.BAD_CLAIM_TYPE,
            f"JWT 声明 {key!r} 必须是 NumericDate（数字）",
            context={"field": key, "actual_type": type(v).__name__},
        )
    return v


def _num(v: float) -> int | float:
    return int(v) if float(v).is_integer() else round(v, 3)


class JwtVerifier:
    def __init__(
        self,
        trust_store: TrustStore,
        caches: JwksCacheRegistry,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = trust_store
        self._caches = caches
        self._clock = clock

    # ------------------------------------------------------------------ #
    # 公开入口
    # ------------------------------------------------------------------ #
    def verify(self, token: str) -> dict[str, Any]:
        """成功返回 ``{"claims", "issuer_id", "kid", "alg"}``。

        任何形态/内容异常都走 :class:`VerifyError`，不向上泄漏底层异常。
        """
        if not isinstance(token, str) or not token:
            raise VerifyError(ErrCode.MALFORMED_TOKEN, "令牌必须是非空字符串")
        if token.count(".") != 2:
            raise VerifyError(
                ErrCode.MALFORMED_TOKEN,
                "JWT 紧凑序列化必须恰好包含 2 个 '.'（3 段）",
                context={"segments": token.count(".") + 1},
            )

        header_b64, payload_b64, signature_b64 = token.split(".")
        signing_input = f"{header_b64}.{payload_b64}".encode("ascii")

        header = self._parse_header(header_b64)
        payload = self._parse_payload(payload_b64)
        # 签名段先做 base64url 解码，但“空签名”延迟到算法白名单检查之后：
        # 否则 alg=none 会被报成 invalid_signature 而不是 alg_not_allowed。
        signature = b64url_decode(signature_b64, field_name="JWT 签名段")

        alg = _require_str_field(header, "alg", what="JWT 头", in_header=True)
        kid = self._check_kid(header)
        self._check_forbidden_params(header)
        self._check_crit(header)
        claims = self._check_claims_shape(payload)

        cfg = self._store.get(claims["iss"])
        if cfg is None:
            raise VerifyError(
                ErrCode.UNKNOWN_ISSUER,
                f"发行方 iss={claims['iss']!r} 不在服务端受信列表中",
                context={"iss": claims["iss"]},
            )
        if alg not in cfg.allowed_algs:
            raise VerifyError(
                ErrCode.ALG_NOT_ALLOWED,
                f"算法 {alg!r} 不在发行方 {cfg.id!r} 的显式允许列表中",
                context={
                    "alg": alg,
                    "issuer_id": cfg.id,
                    "allowed_algs": sorted(cfg.allowed_algs),
                },
            )

        if not signature:
            raise VerifyError(
                ErrCode.INVALID_SIGNATURE,
                "JWT 签名段为空（不接受 alg=none 式无签名令牌）",
            )

        if alg in SYM_ALGORITHMS:
            self._verify_hmac(cfg, alg, signing_input, signature)
        else:
            self._verify_asymmetric(cfg, alg, kid, signing_input, signature)

        self._verify_times(cfg, claims)
        self._verify_aud(cfg, claims)

        return {
            "claims": claims,
            "issuer_id": cfg.id,
            "kid": kid,
            "alg": alg,
        }

    # ------------------------------------------------------------------ #
    # 结构解析
    # ------------------------------------------------------------------ #
    def _parse_header(self, seg: str) -> dict[str, Any]:
        data = parse_json_strict(
            b64url_decode(seg, field_name="JWT 头"), what="JWT 头"
        )
        if not isinstance(data, dict):
            raise VerifyError(
                ErrCode.MALFORMED_HEADER, "JWT 头的 JSON 顶层必须是对象"
            )
        typ = data.get("typ", "JWT")
        if not isinstance(typ, str) or typ not in {
            "JWT", "jwt", "at+jwt", "application/at+jwt"
        }:
            raise VerifyError(
                ErrCode.BAD_HEADER_FIELD,
                f"JWT 头 typ={typ!r} 不被接受",
            )
        if "zip" in data:
            raise VerifyError(
                ErrCode.UNSUPPORTED_COMPRESSION,
                "JWT 头声明了 zip 压缩，本服务不接受嵌套/压缩 JWT",
                context={"zip": data["zip"]},
            )
        if "cty" in data and data["cty"] not in ("JWT", "jwt"):
            raise VerifyError(
                ErrCode.UNSUPPORTED_COMPRESSION,
                f"JWT 头 cty={data['cty']!r} 不是嵌套 JWT，本服务拒绝",
            )
        return data

    def _parse_payload(self, seg: str) -> dict[str, Any]:
        data = parse_json_strict(
            b64url_decode(seg, field_name="JWT 载荷"), what="JWT 载荷"
        )
        if not isinstance(data, dict):
            raise VerifyError(
                ErrCode.MALFORMED_PAYLOAD, "JWT 载荷的 JSON 顶层必须是对象"
            )
        return data

    def _check_kid(self, header: dict[str, Any]) -> str:
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise VerifyError(
                ErrCode.MISSING_HEADER_FIELD,
                "JWT 头必须携带非空字符串 kid（多发行方/多密钥环境强制要求）",
                context={"field": "kid"},
            )
        return kid

    def _check_forbidden_params(self, header: dict[str, Any]) -> None:
        for p in _FORBIDDEN_HEADER_PARAMS:
            if p in header:
                raise VerifyError(
                    ErrCode.HEADER_PARAMETER_FORBIDDEN,
                    f"JWT 头携带 {p!r}：禁止由令牌指定取钥地址或内嵌公钥，"
                    "验证方只使用服务端配置的 JWKS 端点",
                    context={"parameter": p},
                )

    def _check_crit(self, header: dict[str, Any]) -> None:
        crit = header.get("crit")
        if crit is None:
            return
        if not isinstance(crit, list) or not all(
            isinstance(c, str) for c in crit
        ):
            raise VerifyError(
                ErrCode.BAD_HEADER_FIELD,
                "JWT 头 crit 必须是字符串数组",
            )
        if crit:
            # 本实现不理解任何关键扩展：出现即必须拒绝（RFC 7515 §4.1.11）。
            raise VerifyError(
                ErrCode.UNRECOGNIZED_CRIT,
                f"JWT 头 crit 列出了验证方不理解的关键扩展: {crit}",
                context={"crit": crit},
            )

    def _check_claims_shape(self, payload: dict[str, Any]) -> dict[str, Any]:
        _require_str_field(payload, "iss", what="JWT 载荷", in_header=False)
        _require_str_field(payload, "sub", what="JWT 载荷", in_header=False)
        _numeric_timestamp(payload, "exp", required=True)
        _numeric_timestamp(payload, "nbf", required=False)
        if "iat" in payload:
            _numeric_timestamp(payload, "iat", required=False)
        aud = payload.get("aud")
        if not isinstance(aud, str) and not (
            isinstance(aud, list)
            and aud
            and all(isinstance(a, str) and a for a in aud)
        ):
            raise VerifyError(
                ErrCode.BAD_CLAIM_TYPE,
                "JWT 声明 'aud' 必须是非空字符串或非空字符串数组",
                context={"field": "aud"},
            )
        return payload

    # ------------------------------------------------------------------ #
    # 验签
    # ------------------------------------------------------------------ #
    def _hmac_key(self, cfg: IssuerConfig) -> bytes:
        if cfg.hmac_secret_b64:
            key = b64url_decode(
                cfg.hmac_secret_b64, field_name="发行方 HMAC 密钥(b64)"
            )
        else:
            assert cfg.hmac_secret is not None
            key = cfg.hmac_secret.encode("utf-8")
        if len(key) < _HMAC_MIN_KEY_BYTES:
            raise VerifyError(
                ErrCode.JWKS_KEY_INVALID,
                f"发行方 {cfg.id!r} 的 HMAC 密钥不足 {_HMAC_MIN_KEY_BYTES} 字节",
            )
        return key

    def _verify_hmac(
        self,
        cfg: IssuerConfig,
        alg: str,
        signing_input: bytes,
        signature: bytes,
    ) -> None:
        key = self._hmac_key(cfg)
        expected = hmac.new(
            key, signing_input, _HMAC_DIGEST[alg]
        ).digest()
        if not hmac.compare_digest(expected, signature):
            raise VerifyError(
                ErrCode.INVALID_SIGNATURE,
                "HMAC 签名不匹配",
                context={"alg": alg, "issuer_id": cfg.id},
            )

    def _verify_asymmetric(
        self,
        cfg: IssuerConfig,
        alg: str,
        kid: str,
        signing_input: bytes,
        signature: bytes,
    ) -> None:
        cache = self._caches.for_issuer(cfg)
        key, jwk_alg = cache.get_key(kid)

        # JWK 自带 alg 约束时必须与令牌头一致。
        if jwk_alg is not None and jwk_alg != alg:
            raise VerifyError(
                ErrCode.KEY_ALG_MISMATCH,
                f"kid={kid!r} 的 JWK 限定 alg={jwk_alg!r}，与令牌头 {alg!r} 不符",
                context={"kid": kid, "token_alg": alg, "jwk_alg": jwk_alg},
            )
        # 密钥族必须与算法匹配 —— 算法混淆的最后一道闸门。
        expected_kty = _ALG_KTY[alg]
        if expected_kty == "RSA" and not isinstance(key, rsa.RSAPublicKey):
            raise VerifyError(
                ErrCode.KEY_ALG_MISMATCH,
                f"alg={alg!r} 需要 RSA 公钥，kid={kid!r} 实际不是",
                context={"kid": kid, "alg": alg},
            )
        if expected_kty == "EC" and not isinstance(
            key, ec.EllipticCurvePublicKey
        ):
            raise VerifyError(
                ErrCode.KEY_ALG_MISMATCH,
                f"alg={alg!r} 需要 EC 公钥，kid={kid!r} 实际不是",
                context={"kid": kid, "alg": alg},
            )
        if expected_kty == "OKP" and not isinstance(key, Ed25519PublicKey):
            raise VerifyError(
                ErrCode.KEY_ALG_MISMATCH,
                f"alg={alg!r} 需要 Ed25519 公钥，kid={kid!r} 实际不是",
                context={"kid": kid, "alg": alg},
            )
        if alg.startswith("ES"):
            curve_name = _ALG_CURVE[alg]
            if not isinstance(key.curve, type(_EC_CURVES[curve_name])):
                raise VerifyError(
                    ErrCode.KEY_ALG_MISMATCH,
                    f"alg={alg!r} 需要 {curve_name} 曲线，kid={kid!r} 曲线不匹配",
                    context={"kid": kid, "alg": alg},
                )

        try:
            if alg.startswith("RS"):
                key.verify(
                    signature, signing_input,
                    padding.PKCS1v15(), _hash_factory(alg)(),
                )
            elif alg.startswith("PS"):
                hash_cls = _hash_factory(alg)
                key.verify(
                    signature, signing_input,
                    padding.PSS(
                        mgf=padding.MGF1(hash_cls()),
                        salt_length=hash_cls.digest_size,
                    ),
                    hash_cls(),
                )
            elif alg.startswith("ES"):
                r_int, s_int = ec_signature_to_dss(signature, key.curve)
                key.verify(
                    encode_dss_signature(r_int, s_int),
                    signing_input,
                    ec.ECDSA(_hash_factory(alg)()),
                )
            elif alg == "EdDSA":
                key.verify(signature, signing_input)
            else:  # 防御性分支：白名单之外的算法根本到不了这里
                raise VerifyError(
                    ErrCode.ALG_NOT_ALLOWED, f"算法 {alg!r} 无验证实现"
                )
        except InvalidSignature:
            raise VerifyError(
                ErrCode.INVALID_SIGNATURE,
                "签名验证未通过（密钥不匹配或内容被篡改）",
                context={"alg": alg, "kid": kid, "issuer_id": cfg.id},
            ) from None

    # ------------------------------------------------------------------ #
    # 声明校验
    # ------------------------------------------------------------------ #
    def _verify_times(self, cfg: IssuerConfig, claims: dict[str, Any]) -> None:
        now = self._clock()
        leeway = cfg.leeway

        exp = claims["exp"]
        # 边界：exp == now 即视为已过期（RFC 7519: 当前时间必须早于 exp）。
        if not now < exp + leeway:
            raise VerifyError(
                ErrCode.TOKEN_EXPIRED,
                f"令牌已过期（exp={exp}，当前={_num(now)}，leeway={leeway}s）",
                context={
                    "exp": exp,
                    "now": _num(now),
                    "leeway_s": leeway,
                    "overdue_s": _num(now - exp),
                },
            )

        nbf = claims.get("nbf")
        if nbf is not None:
            # 边界：now == nbf 恰好生效（当前时间不得早于 nbf）。
            if now + leeway < nbf:
                raise VerifyError(
                    ErrCode.TOKEN_NOT_YET_VALID,
                    f"令牌尚未生效（nbf={nbf}，当前={_num(now)}，"
                    f"leeway={leeway}s）",
                    context={
                        "nbf": nbf,
                        "now": _num(now),
                        "leeway_s": leeway,
                        "wait_s": _num(nbf - now),
                    },
                )

        iat = claims.get("iat")
        if iat is not None and iat - leeway > now:
            # iat 在未来通常意味着时钟问题或伪造；拒绝。
            raise VerifyError(
                ErrCode.TOKEN_NOT_YET_VALID,
                f"令牌签发时间 iat={iat} 晚于当前时间",
                context={"iat": iat, "now": _num(now), "leeway_s": leeway},
            )

    def _verify_aud(self, cfg: IssuerConfig, claims: dict[str, Any]) -> None:
        aud = claims["aud"]
        token_audiences = [aud] if isinstance(aud, str) else list(aud)
        if not any(a in cfg.audiences for a in token_audiences):
            raise VerifyError(
                ErrCode.AUD_NOT_ALLOWED,
                "令牌 aud 与该发行方允许的受众无交集",
                context={
                    "token_aud": token_audiences,
                    "expected_any_of": sorted(cfg.audiences),
                },
            )
