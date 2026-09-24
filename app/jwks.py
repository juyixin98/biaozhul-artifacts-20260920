"""JWKS 获取、解析与按发行方隔离的本地缓存。

安全要点：
- 取钥地址只来自服务端 :class:`~app.config.IssuerConfig`，调用方/令牌无法影响；
- 每个发行方一把独立的锁、一份独立缓存，互不污染；
- 远程 JWKS 中若出现重复 ``kid`` 直接整份拒绝（无法判断该用哪把钥匙）；
- 刷新失败且 kid 不在旧缓存中时拒绝验签；旧缓存可用则降级使用；
- 刷新冷却避免发行方端点故障时被请求打爆。
"""

from __future__ import annotations

import base64
import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.ec import (
    EllipticCurve,
    EllipticCurvePublicNumbers,
    SECP256R1,
    SECP384R1,
    SECP521R1,
)
from cryptography.hazmat.primitives.asymmetric.rsa import (
    RSAPublicNumbers,
)

from .config import IssuerConfig
from .errors import ErrCode, VerifyError

# alg -> 期望的 JWK kty（精确匹配，防 alg/key 族混淆）。
_ALG_KTY: dict[str, str] = {
    "RS256": "RSA", "RS384": "RSA", "RS512": "RSA",
    "PS256": "RSA", "PS384": "RSA", "PS512": "RSA",
    "ES256": "EC", "ES384": "EC", "ES512": "EC",
    "EdDSA": "OKP",
}
_ALG_CURVE = {
    "ES256": "P-256",
    "ES384": "P-384",
    "ES512": "P-521",
}
_EC_CURVES: dict[str, EllipticCurve] = {
    "P-256": SECP256R1(),
    "P-384": SECP384R1(),
    "P-521": SECP521R1(),
}

_FETCH_TIMEOUT = 5.0
_MAX_JWKS_BYTES = 256 * 1024
# 空缓存时拉取失败的最小重试间隔。
_FAIL_COOLDOWN = 10.0


def b64url_decode(data: str | bytes, *, field_name: str) -> bytes:
    """无填充 base64url 解码，非法输入给出结构化错误。

    标准库 ``urlsafe_b64decode`` 会静默丢弃字母表外字符，这里先做严格
    字符集校验，避免 ``@@@`` 之类输入被当成空串继续流转。
    """
    if isinstance(data, str):
        data = data.encode("ascii", errors="strict")
    allowed = set(
        b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    )
    if any(c not in allowed for c in data):
        raise VerifyError(
            ErrCode.BAD_BASE64URL,
            f"{field_name} 含 base64url 字母表之外的字符",
        )
    pad = b"=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(data + pad)
    except Exception as e:  # binascii.Error / ValueError
        raise VerifyError(
            ErrCode.BAD_BASE64URL,
            f"{field_name} 不是合法的 base64url 编码",
        ) from e


def _json_object_hook_no_dup(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """拒绝重复 JSON 键（RFC 8259 SHOULD），防止解析差异绕过校验。"""
    obj: dict[str, Any] = {}
    for k, v in pairs:
        if k in obj:
            raise VerifyError(
                ErrCode.DUPLICATE_JSON_KEY,
                f"JSON 对象中出现重复键: {k!r}",
                context={"key": k},
            )
        obj[k] = v
    return obj


def parse_json_strict(data: bytes, *, what: str) -> Any:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise VerifyError(
            ErrCode.BAD_JSON, f"{what} 不是合法 UTF-8 文本"
        ) from e
    try:
        return json.loads(text, object_pairs_hook=_json_object_hook_no_dup)
    except json.JSONDecodeError as e:
        raise VerifyError(
            ErrCode.BAD_JSON, f"{what} 不是合法 JSON: {e.msg}",
            context={"line": e.lineno, "column": e.colno},
        ) from e


def _b64_uint(data_b64: str, *, what: str) -> int:
    raw = b64url_decode(data_b64, field_name=what)
    if not raw:
        raise VerifyError(ErrCode.JWKS_KEY_INVALID, f"{what} 不能为空")
    return int.from_bytes(raw, "big", signed=False)


def jwk_to_public_key(jwk: dict[str, Any]) -> tuple[Any, str | None]:
    """把单个 JWK 转成 cryptography 公钥对象，返回 (key, alg约束)。"""
    if not isinstance(jwk, dict):
        raise VerifyError(ErrCode.JWKS_KEY_INVALID, "JWK 必须是 JSON 对象")

    kty = jwk.get("kty")
    if not isinstance(kty, str):
        raise VerifyError(
            ErrCode.JWKS_KEY_INVALID, "JWK 缺少字符串类型的 kty"
        )
    use = jwk.get("use")
    if use is not None and use != "sig":
        raise VerifyError(
            ErrCode.JWKS_KEY_INVALID,
            f"JWK 的 use={use!r} 不是 sig，不能用于验签",
            context={"kty": kty},
        )
    key_ops = jwk.get("key_ops")
    if key_ops is not None:
        if not isinstance(key_ops, list) or "verify" not in key_ops:
            raise VerifyError(
                ErrCode.JWKS_KEY_INVALID,
                "JWK 的 key_ops 未包含 verify，不能用于验签",
                context={"kty": kty},
            )
    alg_constraint = jwk.get("alg")
    if alg_constraint is not None and not isinstance(alg_constraint, str):
        raise VerifyError(
            ErrCode.JWKS_KEY_INVALID, "JWK 的 alg 必须是字符串"
        )

    if kty == "RSA":
        try:
            n = _b64_uint(jwk["n"], what="RSA JWK.n")
            e = _b64_uint(jwk["e"], what="RSA JWK.e")
            key = RSAPublicNumbers(e, n).public_key()
        except KeyError as e:
            raise VerifyError(
                ErrCode.JWKS_KEY_INVALID, f"RSA JWK 缺少字段 {e.args[0]}"
            ) from e
        except (ValueError, TypeError) as e:
            raise VerifyError(
                ErrCode.JWKS_KEY_INVALID, f"RSA JWK 参数无效: {e}"
            ) from e
    elif kty == "EC":
        crv = jwk.get("crv")
        if crv not in _EC_CURVES:
            raise VerifyError(
                ErrCode.JWKS_KEY_INVALID,
                f"EC JWK 的 crv 不受支持: {crv!r}",
            )
        try:
            x = b64url_decode(jwk["x"], field_name="EC JWK.x")
            y = b64url_decode(jwk["y"], field_name="EC JWK.y")
        except KeyError as e:
            raise VerifyError(
                ErrCode.JWKS_KEY_INVALID, f"EC JWK 缺少字段 {e.args[0]}"
            ) from e
        curve = _EC_CURVES[crv]
        try:
            key = EllipticCurvePublicNumbers(
                x=int.from_bytes(x, "big"),
                y=int.from_bytes(y, "big"),
                curve=curve,
            ).public_key()
        except (ValueError, TypeError) as e:
            raise VerifyError(
                ErrCode.JWKS_KEY_INVALID, f"EC JWK 参数无效: {e}"
            ) from e
    elif kty == "OKP":
        crv = jwk.get("crv")
        if crv != "Ed25519":
            raise VerifyError(
                ErrCode.JWKS_KEY_INVALID,
                f"OKP JWK 的 crv 不受支持: {crv!r}（仅 Ed25519）",
            )
        try:
            x = b64url_decode(jwk["x"], field_name="OKP JWK.x")
        except KeyError as e:
            raise VerifyError(
                ErrCode.JWKS_KEY_INVALID, f"OKP JWK 缺少字段 {e.args[0]}"
            ) from e
        try:
            key = Ed25519PublicKey.from_public_bytes(x)
        except (ValueError, TypeError) as e:
            raise VerifyError(
                ErrCode.JWKS_KEY_INVALID, f"Ed25519 JWK 参数无效: {e}"
            ) from e
    else:
        raise VerifyError(
            ErrCode.JWKS_KEY_INVALID,
            f"JWK kty 不受支持: {kty!r}（支持 RSA / EC / OKP）",
        )

    # 私钥材料绝不该出现在 JWKS，出现即视为异常。
    if any(k in jwk for k in ("d", "p", "q", "dp", "dq", "qi", "k")):
        raise VerifyError(
            ErrCode.JWKS_KEY_INVALID,
            "JWKS 条目中出现了私钥/对称密钥字段，拒绝使用",
            context={"kty": kty},
        )
    return key, alg_constraint


def parse_jwks(body: bytes) -> dict[str, tuple[Any, str | None]]:
    """解析 JWKS 文档为 ``{kid: (public_key, alg约束)}``。

    - kid 缺失或非字符串：拒绝整份文档；
    - 同一文档内 kid 重复：拒绝整份文档（:data:`ErrCode.DUPLICATE_KID_IN_JWKS`）。
    """
    doc = parse_json_strict(body, what="JWKS 文档")
    if not isinstance(doc, dict) or not isinstance(doc.get("keys"), list):
        raise VerifyError(
            ErrCode.JWKS_MALFORMED, 'JWKS 文档必须是含 "keys" 数组的对象'
        )

    out: dict[str, tuple[Any, str | None]] = {}
    for i, jwk in enumerate(doc["keys"]):
        if not isinstance(jwk, dict):
            raise VerifyError(
                ErrCode.JWKS_KEY_INVALID,
                f"JWKS keys[{i}] 不是对象",
                context={"index": i},
            )
        kid = jwk.get("kid")
        if not isinstance(kid, str) or not kid:
            raise VerifyError(
                ErrCode.JWKS_KEY_INVALID,
                f"JWKS keys[{i}] 缺少非空字符串 kid",
                context={"index": i},
            )
        if kid in out:
            raise VerifyError(
                ErrCode.DUPLICATE_KID_IN_JWKS,
                f"JWKS 文档中 kid 重复: {kid!r}，无法确定验签密钥",
                context={"kid": kid},
            )
        out[kid] = jwk_to_public_key(jwk)
    if not out:
        raise VerifyError(ErrCode.JWKS_MALFORMED, "JWKS 文档不含任何密钥")
    return out


@dataclass
class FetchedJwks:
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)


class JwksFetcher(Protocol):
    def __call__(self, uri: str) -> FetchedJwks: ...


class UrllibJwksFetcher:
    """生产环境用的 urllib 抓取器：限时、限长、不跟随重定向。"""

    def __call__(self, uri: str) -> FetchedJwks:
        req = urllib.request.Request(
            uri, headers={"Accept": "application/json", "User-Agent": "jwt-gateway"}
        )

        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a: Any, **kw: Any) -> None:
                return None

        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(req, timeout=_FETCH_TIMEOUT) as resp:
                if resp.status != 200:
                    raise VerifyError(
                        ErrCode.JWKS_FETCH_FAILED,
                        f"JWKS 端点返回 HTTP {resp.status}",
                    )
                body = resp.read(_MAX_JWKS_BYTES + 1)
                headers = {k.lower(): v for k, v in resp.headers.items()}
        except urllib.error.HTTPError as e:
            raise VerifyError(
                ErrCode.JWKS_FETCH_FAILED,
                f"JWKS 端点返回 HTTP {e.code}",
            ) from e
        except (urllib.error.URLError, OSError) as e:
            raise VerifyError(
                ErrCode.JWKS_FETCH_FAILED, f"JWKS 端点不可达: {e.reason if isinstance(e, urllib.error.URLError) else e}"
            ) from e

        if len(body) > _MAX_JWKS_BYTES:
            raise VerifyError(
                ErrCode.JWKS_FETCH_FAILED,
                f"JWKS 文档超过 {_MAX_JWKS_BYTES} 字节上限",
            )
        return FetchedJwks(body=body, headers=headers)


def _cache_max_age(headers: dict[str, str], configured_ttl: int) -> int:
    """尊重 Cache-Control: max-age，但只用于**缩短**本地 TTL。"""
    cc = headers.get("cache-control", "")
    for part in cc.split(","):
        part = part.strip().lower()
        if part.startswith("max-age="):
            try:
                return max(0, min(configured_ttl, int(part.split("=", 1)[1])))
            except ValueError:
                break
    return configured_ttl


class IssuerJwksCache:
    """单个发行方的缓存。每个发行方一个实例，彼此完全隔离。"""

    def __init__(
        self,
        config: IssuerConfig,
        fetcher: JwksFetcher,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._cfg = config
        self._fetch = fetcher
        self._clock = clock
        self._lock = threading.Lock()
        self._keys: dict[str, tuple[Any, str | None]] = {}
        self._fetched_at = 0.0
        self._ttl = config.cache_ttl
        self._last_fail_at: float | None = None

    def _fresh(self) -> bool:
        return bool(self._keys) and self._clock() - self._fetched_at < self._ttl

    def _refresh_locked(self) -> None:
        """调用方持锁。失败时：有旧钥匙保留旧钥匙；无旧钥匙进入冷却。"""
        assert self._cfg.jwks_uri is not None
        fetched = self._fetch(self._cfg.jwks_uri)
        keys = parse_jwks(fetched.body)
        # 全部解析通过后整体替换 —— 不会出现半份新密钥污染。
        self._keys = keys
        self._ttl = _cache_max_age(fetched.headers, self._cfg.cache_ttl)
        self._fetched_at = self._clock()
        self._last_fail_at = None

    def get_key(
        self, kid: str, *, force_refresh: bool = False
    ) -> tuple[Any, str | None]:
        with self._lock:
            need_refresh = (
                force_refresh
                or not self._keys
                or not self._fresh()
            )
            if need_refresh:
                # 空缓存 + 刚失败过：处于冷却窗口，直接返回失败原因。
                if (
                    not self._keys
                    and self._last_fail_at is not None
                    and self._clock() - self._last_fail_at < _FAIL_COOLDOWN
                ):
                    raise VerifyError(
                        ErrCode.JWKS_FETCH_FAILED,
                        "JWKS 拉取失败且无缓存可用（冷却中）",
                        context={"kid": kid, "cooldown_s": _FAIL_COOLDOWN},
                    )
                try:
                    self._refresh_locked()
                except VerifyError:
                    self._last_fail_at = self._clock()
                    if kid in self._keys:
                        # 有旧缓存且命中：降级使用旧钥匙，记录在 context 里。
                        key, alg = self._keys[kid]
                        return key, alg
                    raise

            try:
                return self._keys[kid]
            except KeyError:
                # 缓存是新鲜的仍找不到 kid：不要无限刷新。
                if self._fresh() and not force_refresh:
                    raise VerifyError(
                        ErrCode.KID_NOT_FOUND,
                        f"发行方 JWKS 中不存在 kid={kid!r}",
                        context={"kid": kid, "cached_keys": len(self._keys)},
                    ) from None
                # 缓存过期导致的未命中已在上面刷新过一次；force 路径再试一次。
                if force_refresh:
                    self._refresh_locked()
                    try:
                        return self._keys[kid]
                    except KeyError:
                        pass
                raise VerifyError(
                    ErrCode.KID_NOT_FOUND,
                    f"发行方 JWKS 中不存在 kid={kid!r}",
                    context={"kid": kid, "cached_keys": len(self._keys)},
                ) from None

    def force_refresh(self) -> int:
        with self._lock:
            self._refresh_locked()
            return len(self._keys)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "cached_keys": len(self._keys),
                "cached_kids": sorted(self._keys),
                "age_s": int(self._clock() - self._fetched_at)
                if self._keys
                else None,
                "ttl_s": self._ttl,
                "fresh": self._fresh(),
                "jwks_uri": self._cfg.jwks_uri,
            }


class JwksCacheRegistry:
    """按发行方配置 id 注册的缓存集合 —— 缓存隔离的边界。"""

    def __init__(
        self,
        fetcher: JwksFetcher | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._fetcher = fetcher or UrllibJwksFetcher()
        self._clock = clock
        self._caches: dict[str, IssuerJwksCache] = {}
        self._lock = threading.Lock()

    def for_issuer(self, cfg: IssuerConfig) -> IssuerJwksCache:
        with self._lock:
            cache = self._caches.get(cfg.id)
            if cache is None:
                cache = IssuerJwksCache(
                    cfg, self._fetcher, clock=self._clock
                )
                self._caches[cfg.id] = cache
            return cache

    def all_snapshots(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {k: c.snapshot() for k, c in self._caches.items()}


# 供测试/签发工具复用：EC JWS 签名是 R||S 定长拼接，需要转 DER。
def ec_signature_to_dss(raw_sig: bytes, curve: EllipticCurve) -> tuple[int, int]:
    size = (curve.key_size + 7) // 8
    if len(raw_sig) != 2 * size:
        raise VerifyError(
            ErrCode.INVALID_SIGNATURE,
            f"EC 签名长度应为 {2 * size} 字节，实际 {len(raw_sig)} 字节",
        )
    r = int.from_bytes(raw_sig[:size], "big")
    s = int.from_bytes(raw_sig[size:], "big")
    return r, s
