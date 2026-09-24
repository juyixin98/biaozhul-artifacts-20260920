"""发行方配置。

发行方信任关系**只能来自服务端配置文件**：每个发行方有独立 id、声明值
``iss``、受信 JWKS 地址 ``jwks_uri``（或对称密钥配置）、允许的算法白名单、
期望的 aud 集合、时钟偏移容忍秒数与缓存 TTL。

令牌自身的 ``iss`` 只用于**索引**这份受信配置；令牌头里出现的 ``jku`` /
``x5u`` / ``jwk`` 等取钥地址一律拒绝（见 verifier），绝不据其发起网络请求。
"""

from __future__ import annotations

import ipaddress
import json
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .errors import VerifyError, ErrCode

# 本网关实现的全部验签算法。配置中只能从这里面挑。
SUPPORTED_ALGORITHMS = frozenset(
    {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512",
     "ES256", "ES384", "ES512", "EdDSA",
     "HS256", "HS384", "HS512"}
)
# 非对称族（密钥来自 JWKS）与对称族（密钥来自本地配置）。
ASYM_ALGORITHMS = frozenset(
    {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512",
     "ES256", "ES384", "ES512", "EdDSA"}
)
SYM_ALGORITHMS = frozenset({"HS256", "HS384", "HS512"})

_DEFAULT_ALGS = ("RS256", "ES256", "PS256", "EdDSA")


@dataclass(frozen=True)
class IssuerConfig:
    """单个受信发行方的完整配置。"""

    id: str
    iss: str
    allowed_algs: frozenset[str]
    audiences: frozenset[str]
    leeway: int
    cache_ttl: int
    jwks_uri: str | None = None
    # 对称算法：base64url 编码的共享密钥，或直接给出的 UTF-8 字符串
    # （字符串走 UTF-8 编码，且长度不得低于 32 字节）。
    hmac_secret_b64: str | None = None
    hmac_secret: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def is_symmetric(self) -> bool:
        return bool(self.allowed_algs & SYM_ALGORITHMS)


def _require_str(d: dict[str, Any], key: str, issuer_id: str) -> str:
    v = d.get(key)
    if not isinstance(v, str) or not v:
        raise VerifyError(
            ErrCode.JWKS_MALFORMED,
            f"发行方 {issuer_id!r} 配置项 {key!r} 必须是非空字符串",
        )
    return v


def ensure_safe_jwks_url(url: str) -> None:
    """JWKS 地址安全检查：仅允许 https；明文 http 仅放行环回地址。

    发行方地址由服务端配置决定，这里再做一道纵深防御，避免把令牌或配置
    指向内网地址。
    """

    p = urlparse(url)
    if p.scheme == "https":
        return
    if p.scheme != "http":
        raise VerifyError(
            ErrCode.JWKS_FETCH_FAILED,
            f"JWKS URI scheme 不被允许: {p.scheme!r}（仅允许 https，"
            "http 仅限环回地址）",
        )
    host = p.hostname or ""
    loopback = {"localhost"}
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_loopback:
            return
    except ValueError:
        # 主机名而非 IP：解析后要求全部落在环回段。
        try:
            infos = socket.getaddrinfo(host, None)
        except OSError:
            infos = []
        if infos and all(
            ipaddress.ip_address(i[4][0]).is_loopback for i in infos
        ):
            return
        if host in loopback:
            return
    else:
        return
    raise VerifyError(
        ErrCode.JWKS_FETCH_FAILED,
        "明文 http 的 JWKS URI 仅允许环回地址（localhost / 127.0.0.1 / ::1）",
        context={"host": host},
    )


def _parse_issuer(raw: dict[str, Any]) -> IssuerConfig:
    if not isinstance(raw, dict):
        raise VerifyError(ErrCode.JWKS_MALFORMED, "issuers[] 每项必须是对象")
    iss_id = str(raw.get("id", ""))
    if not iss_id:
        raise VerifyError(ErrCode.JWKS_MALFORMED, "发行方配置缺少非空 id")

    iss = _require_str(raw, "iss", iss_id)

    algs = raw.get("allowed_algs", list(_DEFAULT_ALGS))
    if not isinstance(algs, list) or not algs or not all(
        isinstance(a, str) for a in algs
    ):
        raise VerifyError(
            ErrCode.JWKS_MALFORMED,
            f"发行方 {iss_id!r} 的 allowed_algs 必须是非空字符串数组",
        )
    alg_set = frozenset(algs)
    unknown = alg_set - SUPPORTED_ALGORITHMS
    if unknown:
        raise VerifyError(
            ErrCode.JWKS_MALFORMED,
            f"发行方 {iss_id!r} 配置了本服务不支持的算法: {sorted(unknown)}",
        )
    if not (alg_set & ASYM_ALGORITHMS) and not (alg_set & SYM_ALGORITHMS):
        raise VerifyError(
            ErrCode.JWKS_MALFORMED,
            f"发行方 {iss_id!r} 未配置任何可用算法",
        )

    auds = raw.get("audiences", [])
    if not isinstance(auds, list) or not auds or not all(
        isinstance(a, str) and a for a in auds
    ):
        raise VerifyError(
            ErrCode.JWKS_MALFORMED,
            f"发行方 {iss_id!r} 的 audiences 必须是非空字符串数组",
        )

    leeway = raw.get("leeway", 0)
    if not isinstance(leeway, int) or isinstance(leeway, bool) or leeway < 0:
        raise VerifyError(
            ErrCode.JWKS_MALFORMED,
            f"发行方 {iss_id!r} 的 leeway 必须是非负整数（秒）",
        )
    ttl = raw.get("cache_ttl", 300)
    if not isinstance(ttl, int) or isinstance(ttl, bool) or ttl < 0:
        raise VerifyError(
            ErrCode.JWKS_MALFORMED,
            f"发行方 {iss_id!r} 的 cache_ttl 必须是非负整数（秒）",
        )

    jwks_uri = raw.get("jwks_uri")
    hmac_secret_b64 = raw.get("hmac_secret_b64")
    hmac_secret = raw.get("hmac_secret")
    asym = bool(alg_set & ASYM_ALGORITHMS)
    sym = bool(alg_set & SYM_ALGORITHMS)

    if asym:
        if not isinstance(jwks_uri, str) or not jwks_uri:
            raise VerifyError(
                ErrCode.JWKS_MALFORMED,
                f"发行方 {iss_id!r} 允许非对称算法，必须配置 jwks_uri",
            )
        ensure_safe_jwks_url(jwks_uri)
    if sym:
        if not isinstance(hmac_secret_b64, str) and not isinstance(
            hmac_secret, str
        ):
            raise VerifyError(
                ErrCode.JWKS_MALFORMED,
                f"发行方 {iss_id!r} 允许 HMAC 算法，必须配置 "
                "hmac_secret_b64 或 hmac_secret",
            )
        if isinstance(hmac_secret, str) and len(hmac_secret.encode()) < 32:
            raise VerifyError(
                ErrCode.JWKS_MALFORMED,
                f"发行方 {iss_id!r} 的 hmac_secret 经 UTF-8 编码后不足 32 字节，"
                "请使用足够长的随机密钥或改用 hmac_secret_b64",
            )

    return IssuerConfig(
        id=iss_id,
        iss=iss,
        allowed_algs=alg_set,
        audiences=frozenset(auds),
        leeway=leeway,
        cache_ttl=ttl,
        jwks_uri=jwks_uri,
        hmac_secret_b64=hmac_secret_b64,
        hmac_secret=hmac_secret,
        extra={k: v for k, v in raw.items() if k not in {
            "id", "iss", "allowed_algs", "audiences", "leeway",
            "cache_ttl", "jwks_uri", "hmac_secret_b64", "hmac_secret",
        }},
    )


@dataclass
class TrustStore:
    """全部受信发行方，按配置 id 与令牌 ``iss`` 声明值双索引。"""

    issuers: dict[str, IssuerConfig]
    by_iss: dict[str, IssuerConfig]

    def get(self, iss: str) -> IssuerConfig | None:
        return self.by_iss.get(iss)

    def describe(self) -> list[dict[str, Any]]:
        """管理/自检接口用，不输出任何密钥材料。"""
        return [
            {
                "id": c.id,
                "iss": c.iss,
                "allowed_algs": sorted(c.allowed_algs),
                "audiences": sorted(c.audiences),
                "jwks_uri": c.jwks_uri,
                "symmetric": c.is_symmetric(),
                "leeway": c.leeway,
                "cache_ttl": c.cache_ttl,
            }
            for c in self.issuers.values()
        ]


def load_trust_store(path: str | Path) -> TrustStore:
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except OSError as e:
        raise VerifyError(
            ErrCode.JWKS_MALFORMED, f"无法读取配置文件 {p}: {e}"
        ) from e
    except json.JSONDecodeError as e:
        raise VerifyError(
            ErrCode.JWKS_MALFORMED, f"配置文件不是合法 JSON: {e}"
        ) from e

    if not isinstance(data, dict) or not isinstance(data.get("issuers"), list):
        raise VerifyError(
            ErrCode.JWKS_MALFORMED, "配置文件顶层必须是含 issuers[] 的对象"
        )

    issuers: dict[str, IssuerConfig] = {}
    by_iss: dict[str, IssuerConfig] = {}
    for raw in data["issuers"]:
        cfg = _parse_issuer(raw)
        if cfg.id in issuers:
            raise VerifyError(
                ErrCode.JWKS_MALFORMED,
                f"发行方 id 重复: {cfg.id!r}",
            )
        if cfg.iss in by_iss:
            raise VerifyError(
                ErrCode.JWKS_MALFORMED,
                f"发行方 iss 声明值重复: {cfg.iss!r}",
            )
        issuers[cfg.id] = cfg
        by_iss[cfg.iss] = cfg

    if not issuers:
        raise VerifyError(
            ErrCode.JWKS_MALFORMED, "至少要配置一个受信发行方"
        )
    return TrustStore(issuers=issuers, by_iss=by_iss)
