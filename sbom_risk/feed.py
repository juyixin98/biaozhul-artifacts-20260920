"""本地漏洞馈送（夹具）的加载与签名校验。

馈送是一个带分离签名的 JSON 信封::

    {
      "alg": "ed25519",
      "kid": "test-fixture-key-2026",
      "signature_b64": "<base64 Ed25519 签名>",
      "payload": {
        "feed_version": "2026.09.24",
        "generated_at": "2026-09-24T00:00:00Z",
        "vulnerabilities": [
          {
            "id": "VULN-XYZ",
            "ecosystem": "npm",
            "name": "left-pad",
            "ranges": [">=1.0.0 <2.0.0"],
            "severity": "high",
            "title": "...",
            "references": ["https://example/advisory"]
          }
        ]
      }
    }

签名内容：payload 的规范 JSON（键排序、无多余空白）的 UTF-8 字节，
Ed25519 对其签名（64 字节，base64 编码）。校验使用 cryptography 库的
:class:`cryptography.hazmat.primitives.asymmetric.ed25519.Ed25519PublicKey`。

一条漏洞可带多个 ranges，本实现按“任一区间命中即受影响”处理
（列表元素语义为 OR，元素内部为 AND），签名校验只保证整条记录未被篡改。
"""
from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, Field

from .ranges import RangeParseError, parse_range


class FeedError(ValueError):
    """馈送文件缺失、结构非法、签名不通过或区间无法解析。"""


class Vulnerability(BaseModel):
    id: str = Field(min_length=1)
    ecosystem: str = Field(min_length=1)
    name: str = Field(min_length=1)
    ranges: List[str] = Field(default_factory=list)  # OR of AND-ranges
    severity: Optional[str] = None
    title: Optional[str] = None
    references: List[str] = Field(default_factory=list)


class FeedPayload(BaseModel):
    feed_version: str = Field(min_length=1)
    generated_at: Optional[str] = None
    vulnerabilities: List[Vulnerability] = Field(default_factory=list)


@dataclass(frozen=True)
class LoadedFeed:
    payload: FeedPayload
    verified: bool
    kid: Optional[str]
    path: Path


def canonical_payload_bytes(payload: FeedPayload | dict) -> bytes:
    """规范序列化：键排序、紧凑分隔、UTF-8、不转义非 ASCII。"""
    if isinstance(payload, FeedPayload):
        data = payload.model_dump()
    else:
        data = payload
    return json.dumps(
        data, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def load_public_key(key_path: Path) -> Ed25519PublicKey:
    try:
        pem = key_path.read_bytes()
    except OSError as exc:
        raise FeedError(f"无法读取公钥 {key_path}: {exc}") from exc
    try:
        key = serialization.load_pem_public_key(pem)
    except Exception as exc:
        raise FeedError(f"公钥不是合法的 PEM: {exc}") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise FeedError(f"仅支持 Ed25519 公钥，实际为 {type(key).__name__}")
    return key


def load_feed(feed_path: Path, public_key_path: Path | None, *, allow_unsigned: bool = False) -> LoadedFeed:
    """加载并验证漏洞馈送。

    - 有公钥：信封必须带合法的 Ed25519 签名，否则抛 :class:`FeedError`。
    - 无公钥且 ``allow_unsigned``：加载但 ``verified=False``（仅限本地调试）。
    """
    path = Path(feed_path)
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FeedError(f"无法读取馈送文件 {path}: {exc}") from exc

    try:
        payload_obj = envelope["payload"]
    except (TypeError, KeyError) as exc:
        raise FeedError("馈送信封缺少 payload 字段") from exc

    try:
        payload = FeedPayload.model_validate(payload_obj)
    except Exception as exc:
        raise FeedError(f"馈送 payload 结构非法: {exc}") from exc

    # 提前解析所有区间，避免运行请求才发现夹具语法错误
    for vuln in payload.vulnerabilities:
        for r in vuln.ranges:
            try:
                parse_range(r)
            except RangeParseError as exc:
                raise FeedError(f"漏洞 {vuln.id} 的区间非法: {exc}") from exc

    verified = False
    kid = envelope.get("kid") if isinstance(envelope, dict) else None

    if public_key_path is not None:
        alg = envelope.get("alg") if isinstance(envelope, dict) else None
        if alg != "ed25519":
            raise FeedError(f"不支持的签名算法: {alg!r}（要求 'ed25519'）")
        sig_b64 = envelope.get("signature_b64") if isinstance(envelope, dict) else None
        if not sig_b64:
            raise FeedError("馈送缺少 signature_b64，无法验证完整性")
        try:
            signature = base64.b64decode(sig_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise FeedError("signature_b64 不是合法 base64") from exc
        key = load_public_key(Path(public_key_path))
        try:
            key.verify(signature, canonical_payload_bytes(payload))
        except InvalidSignature as exc:
            raise FeedError("馈送签名校验失败：payload 可能被篡改或公钥不匹配") from exc
        verified = True
    elif not allow_unsigned:
        raise FeedError(
            "未配置公钥且未显式允许未签名馈送（allow_unsigned=False），拒绝加载"
        )

    return LoadedFeed(payload=payload, verified=verified, kid=kid, path=path)
