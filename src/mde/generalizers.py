"""泛化器（generalizer）：把敏感值替换为较低信息量的值。

设计原则
========
* 泛化是**有损披露策略**，不是匿名化手段。README 与输出清单中均明确
  避免“匿名化保证”的表述。
* 哈希使用任务级随机盐（HMAC-SHA256，基于 cryptography/标准库原语），
  同一导出任务内稳定、可做关联，跨任务不可关联；盐随导出包保存（它不
  是秘密——这是假名化而非加密）。
* 所有泛化器必须对输入类型做显式校验，失败抛 GeneralizerError，
  由引擎对该字段执行“失败即拒绝（fail closed）”。
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import re
from typing import Any, Callable

from .errors import GeneralizerError

Generalizer = Callable[[Any, dict[str, Any], bytes], Any]

_REGISTRY: dict[str, Generalizer] = {}


def register(name: str) -> Callable[[Generalizer], Generalizer]:
    def deco(fn: Generalizer) -> Generalizer:
        if name in _REGISTRY:
            raise RuntimeError(f"generalizer {name!r} already registered")
        _REGISTRY[name] = fn
        return fn
    return deco


def get_generalizer(name: str) -> Generalizer:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise GeneralizerError(f"unknown generalizer {name!r}") from None


def _as_str(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool):  # bool 是 int 的子类，先排除
        raise GeneralizerError(f"expected string, got bool: {value!r}")
    if isinstance(value, (int, float)):
        return str(value)
    raise GeneralizerError(f"expected string-like value, got {type(value).__name__}")


@register("redact")
def g_redact(value: Any, params: dict[str, Any], salt: bytes) -> Any:
    """整值删除：泛化结果为 None（输出中字段保留但值为 null）。"""
    return None


@register("mask")
def g_mask(value: Any, params: dict[str, Any], salt: bytes) -> Any:
    """字符串遮罩，保留前 keep_prefix / 后 keep_suffix 个字符。"""
    s = _as_str(value)
    kp = int(params.get("keep_prefix", 0))
    ks = int(params.get("keep_suffix", 4))
    ch = str(params.get("char", "*"))
    if len(ch) != 1:
        raise GeneralizerError("mask 'char' must be a single character")
    if kp < 0 or ks < 0:
        raise GeneralizerError("keep counts must be >= 0")
    if len(s) <= kp + ks:
        # 太短时不保留任何原文，直接整串遮罩，避免反推。
        return ch * len(s) if s else s
    middle_len = len(s) - kp - ks
    return s[:kp] + ch * middle_len + s[len(s) - ks:]


_EMAIL_RE = re.compile(r"^([^@\s]+)@([^@\s]+)$")


@register("email_mask")
def g_email_mask(value: Any, params: dict[str, Any], salt: bytes) -> Any:
    """邮箱掩码：保留首字符与域名，如 ``a***@example.com``。"""
    s = _as_str(value).strip()
    m = _EMAIL_RE.match(s)
    if not m:
        raise GeneralizerError(f"not a valid email address: {value!r}")
    local, domain = m.group(1), m.group(2)
    keep = int(params.get("keep_prefix", 1))
    if keep < 1:
        keep = 1
    if len(local) <= keep:
        masked = local[:1] + "***"
    else:
        masked = local[:keep] + "***"
    return f"{masked}@{domain}"


_DATE_FORMATS = ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d")


@register("date_bucket")
def g_date_bucket(value: Any, params: dict[str, Any], salt: bytes) -> Any:
    """时间粗化：day -> YYYY-MM-DD，month -> YYYY-MM，year -> YYYY。

    数字（Unix 秒）也支持，按 UTC 解释。
    """
    granularity = str(params.get("granularity", "month"))
    dt = _coerce_datetime(value)
    if granularity == "day":
        return dt.strftime("%Y-%m-%d")
    if granularity == "month":
        return dt.strftime("%Y-%m")
    if granularity == "year":
        return dt.strftime("%Y")
    raise GeneralizerError(f"unknown date granularity {granularity!r}")


def _coerce_datetime(value: Any) -> datetime.datetime:
    if isinstance(value, datetime.datetime):
        return value.astimezone(datetime.timezone.utc) if value.tzinfo else value.replace(
            tzinfo=datetime.timezone.utc)
    if isinstance(value, datetime.date):
        return datetime.datetime(value.year, value.month, value.day,
                                 tzinfo=datetime.timezone.utc)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.datetime.fromtimestamp(float(value), tz=datetime.timezone.utc)
    if isinstance(value, str):
        s = value.strip()
        if s.endswith("Z"):
            try:
                return datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=datetime.timezone.utc)
            except ValueError:
                try:
                    d = datetime.date.fromisoformat(s.rstrip("Z"))
                    return datetime.datetime(d.year, d.month, d.day,
                                             tzinfo=datetime.timezone.utc)
                except ValueError:
                    pass
        for fmt in _DATE_FORMATS:
            try:
                dt = datetime.datetime.strptime(s, fmt)
                return dt.replace(tzinfo=datetime.timezone.utc)
            except ValueError:
                continue
        try:
            dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            return dt.astimezone(datetime.timezone.utc)
        except ValueError:
            pass
    raise GeneralizerError(f"cannot interpret value as date: {value!r}")


@register("numeric_bucket")
def g_numeric_bucket(value: Any, params: dict[str, Any], salt: bytes) -> Any:
    """数值分桶，输出标签字符串，如 ``[10,20)``。

    params: {"bins": [0, 18, 30, 65]} 定义左闭右开区间；
    小于最小值为 ``<min``，大于等于最大值为 ``>=max``。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GeneralizerError(f"numeric_bucket expects number, got {value!r}")
    bins = params.get("bins")
    if not isinstance(bins, list) or len(bins) < 2:
        raise GeneralizerError("numeric_bucket requires 'bins' list of >= 2 numbers")
    nums = [float(b) for b in bins]
    if any(nums[i] >= nums[i + 1] for i in range(len(nums) - 1)):
        raise GeneralizerError("bins must be strictly increasing")
    x = float(value)
    if x < nums[0]:
        return f"<{_fmt_num(nums[0])}"
    for i in range(len(nums) - 1):
        if nums[i] <= x < nums[i + 1]:
            return f"[{_fmt_num(nums[i])},{_fmt_num(nums[i + 1])})"
    return f">={_fmt_num(nums[-1])}"


def _fmt_num(x: float) -> str:
    if x == int(x):
        return str(int(x))
    return repr(x)


@register("hash")
def g_hash(value: Any, params: dict[str, Any], salt: bytes) -> Any:
    """假名化哈希：HMAC-SHA256(salt, 规范化值) -> 16 字节 hex。

    注意：这是假名化（pseudonymization），可被持盐者关联，不提供
    匿名性。盐随导出包返回以便核验/复算，不应被视为秘密。
    """
    length = int(params.get("length", 16))
    if not 4 <= length <= 32:
        raise GeneralizerError("hash length must be between 4 and 32 bytes")
    s = _as_str(value)
    digest = hmac.new(salt, s.encode("utf-8"), hashlib.sha256).hexdigest()
    return digest[: length * 2]


@register("category")
def g_category(value: Any, params: dict[str, Any], salt: bytes) -> Any:
    """按映射表归类；未命中落入 default（默认 fail closed）。

    params: {"map": {"CN": "domestic", "US": "foreign"}, "default": "other"}
    """
    mapping = params.get("map")
    if not isinstance(mapping, dict):
        raise GeneralizerError("category requires 'map' object")
    key = value if isinstance(value, str) else (
        str(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    )
    if key is None:
        raise GeneralizerError(f"category expects scalar value, got {value!r}")
    if key in mapping:
        return mapping[key]
    if "default" in params:
        return params["default"]
    raise GeneralizerError(f"category: no mapping for value {key!r}")


@register("replace")
def g_replace(value: Any, params: dict[str, Any], salt: bytes) -> Any:
    """固定常量替换。params: {"with": "<REDACTED>"}。"""
    return params.get("with", "<REDACTED>")
