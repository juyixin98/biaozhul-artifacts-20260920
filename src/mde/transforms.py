"""泛化原语（generalization transforms）。

每个原语是确定性的纯函数，参数在策略发布时校验。
伪名化使用 HMAC-SHA256（成熟原语），密钥来自本地测试密钥，
**不提供匿名化或抗再识别保证** —— 它只是目的绑定（purpose-bound）的假名。
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict

_EMAIL_RE = re.compile(r"^[^@\s]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)$")


class TransformError(ValueError):
    """泛化执行失败（确定性错误，决策记录会保留原因，输出丢弃）。"""


@dataclass
class TransformContext:
    """一次导出任务内所有泛化共享的上下文（含派生密钥缓存）。"""

    transform_secret: bytes
    policy_fingerprint: str
    purpose: str
    _key_cache: Dict[str, bytes] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self._key_cache is None:
            self._key_cache = {}

    def purpose_key(self, purpose: str) -> bytes:
        """HKDF-SHA256 从全局 transform secret 派生目的绑定密钥。

        固定 salt/info，不使用 salt 参数（RFC 5869），保证跨进程可复验。
        """
        if purpose in self._key_cache:
            return self._key_cache[purpose]
        # HKDF-Extract：PRK = HMAC(salt=固定0字节串, IKM=secret)
        prk = hmac.new(b"mde-purpose-bound-v1", self.transform_secret, hashlib.sha256).digest()
        info = f"mde/pseudonymize/v1/purpose={purpose}/policy={self.policy_fingerprint}".encode()
        # HKDF-Expand，单块即可（32 字节）
        okm = hmac.new(prk, info + b"\x01", hashlib.sha256).digest()
        self._key_cache[purpose] = okm
        return okm


# ---- 参数校验：返回规范化后的 params ---------------------------------------

def _check_params(params: Any, required: Dict[str, type]) -> Dict[str, Any]:
    if not isinstance(params, dict):
        raise TransformError("泛化参数必须是对象")
    for k, typ in required.items():
        if k not in params:
            raise TransformError(f"缺少泛化参数: {k}")
        if not isinstance(params[k], typ) or (typ is int and isinstance(params[k], bool)):
            raise TransformError(f"泛化参数 {k} 类型必须为 {typ.__name__}")
    return dict(params)


def _v_email_domain(params: Any) -> Dict[str, Any]:
    p = _check_params(params, {})
    if p:
        raise TransformError("email_domain 不接受参数")
    return {}


def _v_date_trunc(params: Any) -> Dict[str, Any]:
    p = _check_params(params, {"unit": str})
    if p["unit"] not in ("year", "month", "day"):
        raise TransformError("date_trunc 的 unit 只能是 year|month|day")
    extra = set(p) - {"unit"}
    if extra:
        raise TransformError(f"date_trunc 含未知参数: {sorted(extra)}")
    return {"unit": p["unit"]}


def _v_bucket_number(params: Any) -> Dict[str, Any]:
    p = _check_params(params, {"width": int, "offset": int})
    if p["width"] <= 0:
        raise TransformError("bucket_number 的 width 必须为正整数")
    extra = set(p) - {"width", "offset"}
    if extra:
        raise TransformError(f"bucket_number 含未知参数: {sorted(extra)}")
    return {"width": p["width"], "offset": p["offset"]}


def _v_mask_tail(params: Any) -> Dict[str, Any]:
    p = _check_params(params, {"keep_prefix": int, "mask": str})
    if p["keep_prefix"] < 0:
        raise TransformError("mask_tail 的 keep_prefix 不能为负")
    if len(p["mask"]) != 1:
        raise TransformError("mask_tail 的 mask 必须是单个字符")
    if p["mask"] in (".", "%"):
        raise TransformError("mask_tail 的 mask 不能是 '.' 或 '%'")
    extra = set(p) - {"keep_prefix", "mask"}
    if extra:
        raise TransformError(f"mask_tail 含未知参数: {sorted(extra)}")
    return {"keep_prefix": p["keep_prefix"], "mask": p["mask"]}


def _v_pseudonymize(params: Any) -> Dict[str, Any]:
    p = _check_params(params, {"scope": str})
    if p["scope"] not in ("purpose", "global"):
        raise TransformError("pseudonymize 的 scope 只能是 purpose|global")
    extra = set(p) - {"scope"}
    if extra:
        raise TransformError(f"pseudonymize 含未知参数: {sorted(extra)}")
    return {"scope": p["scope"]}


# ---- 执行 ------------------------------------------------------------------

def _t_email_domain(value: Any, params: Dict[str, Any], ctx: TransformContext) -> str:
    if not isinstance(value, str):
        raise TransformError("email_domain 输入必须是字符串")
    m = _EMAIL_RE.match(value.strip())
    if not m:
        raise TransformError("不是合法的邮箱地址")
    return m.group(1).lower()


def _t_date_trunc(value: Any, params: Dict[str, Any], ctx: TransformContext) -> str:
    unit = params["unit"]
    if not isinstance(value, str):
        raise TransformError("date_trunc 输入必须是 ISO 8601 字符串")
    text = value.strip()
    try:
        if unit == "year":
            d = _dt.date.fromisoformat(text[:10] if len(text) >= 10 else text)
            return f"{d.year:04d}"
        d = _dt.date.fromisoformat(text[:10])
    except ValueError as e:
        raise TransformError(f"不是合法的 ISO 8601 日期: {value!r}") from e
    if unit == "month":
        return f"{d.year:04d}-{d.month:02d}"
    return f"{d.year:04d}-{d.month:02d}-{d.day:02d}"


def _t_bucket_number(value: Any, params: Dict[str, Any], ctx: TransformContext) -> str:
    # bool 是 int 子类，显式拒绝，避免 True/False 被当数字
    if isinstance(value, bool) or not isinstance(value, int):
        raise TransformError("bucket_number 输入必须是整数")
    width, offset = params["width"], params["offset"]
    bucket = ((value - offset) // width) * width + offset
    return f"[{bucket},{bucket + width})"


def _t_mask_tail(value: Any, params: Dict[str, Any], ctx: TransformContext) -> str:
    if not isinstance(value, str):
        raise TransformError("mask_tail 输入必须是字符串")
    keep, mask = params["keep_prefix"], params["mask"]
    # 掩码长度固定，避免掩码长度泄露原值长度
    if len(value) <= keep:
        return value + mask * 8
    return value[:keep] + mask * 8


def _canonical_scalar(value: Any) -> bytes:
    if value is None:
        return b"null"
    if isinstance(value, bool):
        return b"true" if value else b"false"
    if isinstance(value, int):
        return f"i:{value}".encode()
    if isinstance(value, float):
        raise TransformError("pseudonymize 不支持浮点（非确定性序列化）")
    if isinstance(value, str):
        return ("s:" + value).encode("utf-8")
    raise TransformError("pseudonymize 只支持标量值")


def _t_pseudonymize(value: Any, params: Dict[str, Any], ctx: TransformContext) -> str:
    data = _canonical_scalar(value)
    key = ctx.purpose_key(ctx.purpose if params["scope"] == "purpose" else "global")
    digest = hmac.new(key, data, hashlib.sha256).hexdigest()
    return f"hmac:{digest[:32]}"


REGISTRY: Dict[str, Callable[[Any, Dict[str, Any], TransformContext], Any]] = {
    "email_domain": _t_email_domain,
    "date_trunc": _t_date_trunc,
    "bucket_number": _t_bucket_number,
    "mask_tail": _t_mask_tail,
    "pseudonymize": _t_pseudonymize,
}

VALIDATORS: Dict[str, Callable[[Any], Dict[str, Any]]] = {
    "email_domain": _v_email_domain,
    "date_trunc": _v_date_trunc,
    "bucket_number": _v_bucket_number,
    "mask_tail": _v_mask_tail,
    "pseudonymize": _v_pseudonymize,
}


def validate_transform(name: str, params: Any) -> Dict[str, Any]:
    if not isinstance(name, str) or name not in REGISTRY:
        raise TransformError(f"未知泛化原语: {name!r}（可用: {sorted(REGISTRY)}）")
    return VALIDATORS[name](params)


def apply_transform(name: str, value: Any, params: Dict[str, Any], ctx: TransformContext) -> Any:
    return REGISTRY[name](value, params, ctx)
