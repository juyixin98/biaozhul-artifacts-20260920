"""规范请求编码（canonical request encoding）。

把请求的 HTTP 方法、请求目标（路径 + 查询串）、正文摘要、时间戳、nonce
归约成一个唯一的、逐字节确定的字节串，供 HMAC 签名使用。

设计原则：**fail closed（宁可拒绝，不做猜测）**。凡是可能让同一段 raw target
产生两种语义的输入（编码的分隔符、双重百分号、控制字符、逃出根的点段、
重复查询键、非法百分号转义），一律抛出 :class:`CanonicalizationError`。
"""

from __future__ import annotations

from .crypto import sha256_hex

# 重新编码时允许“裸写”（不做百分号转义）的字符白名单：RFC 3986 unreserved。
_UNRESERVED = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~"
)

_HEX = b"0123456789ABCDEF"


class CanonicalizationError(ValueError):
    """请求目标无法安全地规范化。"""


def _strict_pct_decode(text: str, *, forbid_slash: bool) -> bytes:
    """对单个 path 段或单个 key/value 做严格百分号解码。

    输入是 ASCII 文本（调用方负责保证），输出解码后的原始字节。

    拒绝条件：
    * ``%`` 后不是两位十六进制数；
    * 解码结果含 NUL / C0 控制字符；
    * ``forbid_slash=True`` 时解码结果含 ``/``（编码的路径分隔符）。
    """
    raw = text.encode("ascii")
    out = bytearray()
    i = 0
    while i < len(raw):
        b = raw[i]
        if b == 0x25:  # '%'
            if i + 2 >= len(raw):
                raise CanonicalizationError("truncated percent-encoding")
            try:
                hi = int(chr(raw[i + 1]), 16)
                lo = int(chr(raw[i + 2]), 16)
            except ValueError:
                raise CanonicalizationError("invalid percent-encoding") from None
            out.append((hi << 4) | lo)
            i += 3
            continue
        out.append(b)
        i += 1

    for b in out:
        if b == 0x2F and forbid_slash:  # '/'
            raise CanonicalizationError(
                "encoded path separator (%2F) is not allowed inside a segment"
            )
        if b < 0x20 or b == 0x7F:
            raise CanonicalizationError("control characters are not allowed")
    return bytes(out)


def _reencode(decoded: bytes) -> str:
    """用 unreserved 白名单把原始字节重新百分号编码（大写十六进制）。

    非 ASCII 的多字节 UTF-8 序列逐字节编码（如中文 → 每个 UTF-8 字节一个 %HH）。
    """
    parts: list[str] = []
    for b in decoded:
        c = chr(b)
        if c in _UNRESERVED:
            parts.append(c)
        else:
            parts.append(f"%{_HEX[b >> 4]:c}{_HEX[b & 0x0F]:c}")
    return "".join(parts)


def canonical_path(path: str) -> str:
    """把 raw path 规范化。

    见 README“路径规范化”一节。返回以 ``/`` 开头的规范路径；
    根路径规范化为 ``/``。
    """
    if not path.startswith("/"):
        raise CanonicalizationError("request path must be absolute (start with '/')")
    # 只允许 ASCII；非 ASCII 必须先由客户端做 UTF-8 百分号编码。
    try:
        path.encode("ascii")
    except UnicodeEncodeError:
        raise CanonicalizationError(
            "non-ASCII characters must be UTF-8 percent-encoded"
        ) from None

    segments = path.split("/")
    # split 后首元素恒为 ""（绝对路径）；逐段处理除首元素外的内容。
    stack: list[str] = []
    for raw_seg in segments[1:]:
        decoded = _strict_pct_decode(raw_seg, forbid_slash=True)

        # 点段归一基于“解码后”的字面量判断，
        # 因此 %2e%2e / %2E%2E 同样会被识别为 ..，不能借编码绕过。
        if decoded in (b".",):
            continue
        if decoded == b"..":
            if not stack:
                raise CanonicalizationError("'..' segment escapes the path root")
            stack.pop()
            continue
        if decoded == b"":
            # 空段（// 或结尾斜杠）折叠掉。
            continue
        stack.append(_reencode(decoded))

    return "/" + "/".join(stack)


def canonical_query(query: str) -> str:
    """把查询串规范化。

    无参数 → ``""``。要求每个参数都是 ``key=value``、无重复键、无裸标记；
    解码后按 (key, value) 字节序排序后重新编码。
    """
    if query == "":
        return ""
    try:
        query.encode("ascii")
    except UnicodeEncodeError:
        raise CanonicalizationError(
            "non-ASCII characters must be UTF-8 percent-encoded"
        ) from None

    pairs: list[tuple[bytes, bytes, str, str]] = []
    seen: set[bytes] = set()
    for part in query.split("&"):
        if "=" not in part:
            raise CanonicalizationError(
                f"query parameter {part!r} is missing '='"
            )
        raw_key, raw_value = part.split("=", 1)
        if raw_key == "":
            raise CanonicalizationError("empty query parameter name")
        key = _strict_pct_decode(raw_key, forbid_slash=False)
        value = _strict_pct_decode(raw_value, forbid_slash=False)
        if key in seen:
            raise CanonicalizationError("duplicate query parameter name")
        seen.add(key)
        pairs.append((key, value, _reencode(key), _reencode(value)))

    pairs.sort(key=lambda item: (item[0], item[1]))
    return "&".join(f"{k}={v}" for _, _, k, v in pairs)


def split_request_target(target: str) -> tuple[str, str]:
    """把 origin-form 请求目标拆成 (raw_path, raw_query)。

    拒绝 fragment、非 origin-form（``http://`` 形式）等形态。
    """
    if target.startswith(("http://", "https://")):
        raise CanonicalizationError("absolute-form request targets are not supported")
    if "#" in target:
        raise CanonicalizationError("fragment is not allowed in request target")
    if "?" in target:
        path, query = target.split("?", 1)
        return path, query
    return target, ""


def canonical_request_target(target: str) -> tuple[str, str]:
    """便捷封装：拆分并规范化请求目标，返回 (canonical_path, canonical_query)。"""
    path, query = split_request_target(target)
    return canonical_path(path), canonical_query(query)


def build_canonical_request(
    *,
    key_id: str,
    method: str,
    canonical_path_value: str,
    canonical_query_value: str,
    body: bytes,
    timestamp: str,
    nonce: str,
) -> str:
    """构造 8 行规范请求串（LF 连接、无尾换行）。"""
    return "\n".join(
        [
            "ANTI-REPLAY-API-HMAC-SHA256-v1",
            key_id,
            method.upper(),
            canonical_path_value,
            canonical_query_value,
            sha256_hex(body),
            timestamp,
            nonce,
        ]
    )
