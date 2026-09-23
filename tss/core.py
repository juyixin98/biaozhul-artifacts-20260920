"""拆分/恢复/校验的高层流程：把信封编解码与 Shamir 运算串起来。

该模块不关心传输方式（HTTP 或 CLI），便于测试与复用。
"""

import base64
from dataclasses import dataclass, field

from .errors import (
    ParameterError,
    ThresholdError,
    DuplicateIndexError,
    ConsistencyError,
    FormatError,
    IntegrityError,
)
from .shamir import SplitParams, SharePoint, split_secret, combine_points, diagnose_points
from .envelope import (
    RawShare,
    encode_share,
    decode_share,
    new_split_id,
    generate_auth_key,
    MIN_AUTH_KEY_BYTES,
)

MAX_SECRET_BYTES = 1024 * 1024  # 1 MiB，本地服务的防 DoS 上限


@dataclass
class SplitResult:
    split_id: str            # hex
    threshold: int
    total: int
    authenticated: bool
    shares: list = field(default_factory=list)  # base64 字符串


@dataclass
class RejectedShare:
    index: int
    reason: str
    detail: str


@dataclass
class RecoverResult:
    secret: bytes
    threshold: int
    total: int
    split_id: str
    authenticated: bool
    used_share_count: int
    rejected: list = field(default_factory=list)  # list[RejectedShare]


def decode_auth_key(auth_key_b64) -> bytes:
    """解码调用方提供的 base64 认证密钥。"""
    if auth_key_b64 is None:
        return None
    if isinstance(auth_key_b64, bytes):
        key = auth_key_b64
    else:
        if not isinstance(auth_key_b64, str):
            raise ParameterError("auth_key must be a base64 string")
        try:
            key = base64.b64decode(auth_key_b64.encode("ascii"), validate=True)
        except Exception as exc:
            raise ParameterError(f"invalid base64 auth_key: {exc}") from exc
    if len(key) < MIN_AUTH_KEY_BYTES:
        raise ParameterError(f"auth_key too short (need >= {MIN_AUTH_KEY_BYTES} bytes)")
    return key


def split(secret, threshold: int, total: int, auth_key=None) -> SplitResult:
    """拆分秘密并输出信封份额。"""
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    if not isinstance(secret, (bytes, bytearray)):
        raise ParameterError("secret must be bytes or a UTF-8 string")
    if len(secret) == 0:
        raise ParameterError("secret must not be empty")
    if len(secret) > MAX_SECRET_BYTES:
        raise ParameterError(f"secret too large (max {MAX_SECRET_BYTES} bytes)")

    params = SplitParams(threshold=threshold, total=total)
    params.validate()

    split_id = new_split_id()
    points = split_secret(bytes(secret), params)
    shares = [
        encode_share(threshold, total, p.x, split_id, p.y, auth_key=auth_key)
        for p in points
    ]
    return SplitResult(
        split_id=split_id.hex(),
        threshold=threshold,
        total=total,
        authenticated=auth_key is not None,
        shares=shares,
    )


def _decode_all(share_texts, auth_key):
    """逐个独立解码；坏份额不影响其他份额的解析。"""
    valid = []   # list[(index, RawShare)]
    rejected = []
    for i, text in enumerate(share_texts):
        try:
            raw = decode_share(text, auth_key=auth_key)
        except (FormatError, IntegrityError, ParameterError) as exc:
            rejected.append(RejectedShare(index=i, reason=exc.code, detail=str(exc)))
            continue
        # 信封声称经过 HMAC 认证，但调用方没有提供密钥：fail-closed，拒绝采信
        if raw.authenticated and auth_key is None:
            rejected.append(RejectedShare(
                index=i,
                reason=IntegrityError.code,
                detail="share is HMAC-authenticated but no auth_key was supplied; "
                       "refusing to trust an unverifiable share",
            ))
            continue
        valid.append((i, raw))
    return valid, rejected


def validate(share_text: str, auth_key=None) -> dict:
    """校验单个份额信封，返回其声明信息（不进行恢复）。"""
    raw = decode_share(share_text, auth_key=auth_key)
    if raw.authenticated and auth_key is None:
        raise IntegrityError(
            "share is HMAC-authenticated but no auth_key was supplied"
        )
    return {
        "version": 1,
        "field": "GF(2^8)-Rijndael-0x11b",
        "threshold": raw.threshold,
        "total": raw.total,
        "x": raw.x,
        "split_id": raw.split_id.hex(),
        "payload_bytes": len(raw.y),
        "authenticated": raw.authenticated,
    }


def recover(share_texts, auth_key=None) -> RecoverResult:
    """从一批信封份额恢复秘密。

    处理顺序：
    1. 逐个独立解码 —— 损坏编码/坏标签记入 rejected，不拖垮整批；
    2. 跨份额一致性 —— 版本、参数、split_id、认证标志必须一致（拒绝混批）；
    3. 检测重复横坐标；
    4. 有效份额 < 阈值 —— 拒绝恢复（ThresholdError）；
    5. == 阈值直接插值；> 阈值做子集投票诊断（启发式，见 :func:`diagnose_points`）。
    """
    if not isinstance(share_texts, (list, tuple)):
        raise ParameterError("shares must be a list of base64 strings")
    if len(share_texts) == 0:
        raise ParameterError("shares list is empty")

    valid, rejected = _decode_all(share_texts, auth_key)

    def attach(exc):
        exc.rejected = [r.__dict__ for r in rejected]
        return exc

    if not valid:
        raise attach(ConsistencyError(
            "no usable shares: every share was malformed or failed integrity check"
        ))

    raws = [r for _, r in valid]
    first = raws[0]

    mismatched = [
        idx for idx, r in valid
        if (r.threshold, r.total) != (first.threshold, first.total)
    ]
    if mismatched:
        raise attach(ConsistencyError(
            f"shares declare different (threshold, total) parameters; "
            f"mismatched request indices: {mismatched}",
            suspect=mismatched,
        ))

    mixed = [idx for idx, r in valid if r.split_id != first.split_id]
    if mixed:
        raise attach(ConsistencyError(
            "shares come from different split instances (split_id mismatch); "
            "refusing to combine shares from mixed batches",
            suspect=mixed,
        ))

    auth_mismatch = [idx for idx, r in valid if r.authenticated != first.authenticated]
    if auth_mismatch:
        raise attach(ConsistencyError(
            "shares mix authenticated and unauthenticated envelopes; "
            "refusing to combine them",
            suspect=auth_mismatch,
        ))

    seen_x = {}
    dupes = []
    for idx, r in valid:
        if r.x in seen_x:
            dupes.extend([seen_x[r.x], idx])
        else:
            seen_x[r.x] = idx
    if dupes:
        exc = DuplicateIndexError(
            f"duplicate share index (x) at request positions: {sorted(set(dupes))}"
        )
        raise attach(exc)

    threshold = first.threshold
    if len(valid) < threshold:
        raise attach(ThresholdError(
            f"{len(valid)} valid share(s) < threshold {threshold}; "
            f"recovery refused"
        ))

    points = [SharePoint(x=r.x, y=r.y) for _, r in valid]
    if len(valid) == threshold:
        secret = combine_points(points, threshold)
    else:
        try:
            secret, _suspect, votes, exhaustive = diagnose_points(points, threshold)
        except ConsistencyError as exc:
            # 子集投票发现不一致：附上解码阶段被拒份额，再上抛
            raise attach(exc)

    return RecoverResult(
        secret=secret,
        threshold=threshold,
        total=first.total,
        split_id=first.split_id.hex(),
        authenticated=first.authenticated,
        used_share_count=len(valid),
        rejected=[r.__dict__ for r in rejected],
    )
