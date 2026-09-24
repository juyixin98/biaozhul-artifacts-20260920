"""四种角色元数据的解析、校验与构建。

角色负载（``signed`` 段落）的结构如下::

    # root —— 信任锚：公钥库 + 各角色的 keyid、阈值、版本与过期时间
    {"_type": "root", "version": 1, "expires": "2030-01-01T00:00:00Z",
     "keys": {"<keyid>": {"keytype": "ed25519", "scheme": "ed25519",
                          "keyval": {"public": "<32 字节公钥 hex>"}}},
     "roles": {"root":      {"keyids": [...], "threshold": 1},
               "targets":   {"keyids": [...], "threshold": 1},
               "snapshot":  {"keyids": [...], "threshold": 1},
               "timestamp": {"keyids": [...], "threshold": 1}}}

    # targets —— 目标文件清单（哈希与长度绑定）
    {"_type": "targets", "version": 1, "expires": ...,
     "targets": {"demo.txt": {"length": 12, "hashes": {"sha256": "..."}}}}

    # snapshot —— 固定 targets 等角色的版本，同时固定 targets 字节
    {"_type": "snapshot", "version": 1, "expires": ...,
     "meta": {"targets.json": {"version": 1, "length": N,
                               "hashes": {"sha256": "..."}}}}

    # timestamp —— 最外层、最频繁轮换；固定 snapshot 的版本与字节
    {"_type": "timestamp", "version": 1, "expires": ...,
     "meta": {"snapshot.json": {"version": 1, "length": N,
                                "hashes": {"sha256": "..."}}}}
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from . import canonical as canonical_mod
from .crypto import KEYTYPE, SCHEME, KeyPair, keyid_for_public_hex
from .errors import MetadataError

ROOT = "root"
TARGETS = "targets"
SNAPSHOT = "snapshot"
TIMESTAMP = "timestamp"
ROLES = (ROOT, TARGETS, SNAPSHOT, TIMESTAMP)

# snapshot / timestamp 的 meta 段中允许出现的键
_META_KEYS = {
    SNAPSHOT: ("targets.json",),
    TIMESTAMP: ("snapshot.json",),
}


# ---------------------------------------------------------------------------
# 基础类型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Metadata:
    """解析后的一份元数据：原始字节、负载字典、签名列表。"""

    raw: bytes
    signed: dict
    signatures: tuple[dict, ...]


@dataclass(frozen=True)
class FileMeta:
    length: int
    sha256: str


# ---------------------------------------------------------------------------:
# 解析与结构校验
# ---------------------------------------------------------------------------


def parse_envelope(raw: bytes, expected_type: str) -> Metadata:
    """解析元数据外层信封并校验 ``signed`` 段落结构。

    结构问题一律抛 :class:`MetadataError`，调用方保证不写入任何状态。
    """

    if not isinstance(raw, (bytes, bytearray)):
        raise MetadataError(f"{expected_type}: 元数据必须是字节串")
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MetadataError(f"{expected_type}: 不是合法的 UTF-8 JSON") from exc

    if not isinstance(envelope, dict):
        raise MetadataError(f"{expected_type}: 顶层必须是对象")
    signed = envelope.get("signed")
    sigs = envelope.get("signatures")
    if not isinstance(signed, dict):
        raise MetadataError(f"{expected_type}: 缺少 signed 对象")
    if not isinstance(sigs, list):
        raise MetadataError(f"{expected_type}: signatures 必须是列表")

    normalized_sigs: list[dict] = []
    seen: set[str] = set()
    for item in sigs:
        if not isinstance(item, dict):
            raise MetadataError(f"{expected_type}: 签名项必须是对象")
        keyid = item.get("keyid")
        sig = item.get("sig")
        if not isinstance(keyid, str) or not isinstance(sig, str):
            raise MetadataError(f"{expected_type}: 签名项缺少 keyid/sig 字符串")
        if keyid in seen:
            raise MetadataError(f"{expected_type}: keyid {keyid} 出现重复签名")
        seen.add(keyid)
        normalized_sigs.append({"keyid": keyid, "sig": sig})

    _check_common(signed, expected_type)
    if expected_type == ROOT:
        _check_root_body(signed)
    elif expected_type == TARGETS:
        _check_targets_body(signed)
    else:
        _check_meta_body(signed, expected_type)

    return Metadata(
        raw=bytes(raw), signed=signed, signatures=tuple(normalized_sigs)
    )


def _check_common(signed: dict, expected_type: str) -> None:
    if signed.get("_type") != expected_type:
        raise MetadataError(
            f"{expected_type}: _type 字段必须为 {expected_type!r}"
        )
    version = signed.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version <= 0:
        raise MetadataError(f"{expected_type}: version 必须是正整数")
    expires = signed.get("expires")
    if not isinstance(expires, str):
        raise MetadataError(f"{expected_type}: expires 必须是字符串")
    parse_expires(expires, expected_type)


def _check_root_body(signed: dict) -> None:
    keys = signed.get("keys")
    if not isinstance(keys, dict) or not keys:
        raise MetadataError("root: keys 必须是非空对象")
    key_pubhex: dict[str, str] = {}
    for keyid, keyobj in keys.items():
        if not isinstance(keyid, str) or len(keyid) != 64:
            raise MetadataError("root: keyid 必须是 64 位十六进制字符串")
        if not isinstance(keyobj, dict):
            raise MetadataError(f"root: keys.{keyid} 必须是对象")
        if keyobj.get("keytype") != KEYTYPE or keyobj.get("scheme") != SCHEME:
            raise MetadataError(f"root: keys.{keyid} 只支持 {KEYTYPE}")
        keyval = keyobj.get("keyval")
        if not isinstance(keyval, dict) or not isinstance(
            keyval.get("public"), str
        ):
            raise MetadataError(f"root: keys.{keyid}.keyval.public 缺失")
        public_hex = keyval["public"]
        if keyid != keyid_for_public_hex(public_hex):
            raise MetadataError(f"root: keys.{keyid} 与公钥推导的 keyid 不一致")
        key_pubhex[keyid] = public_hex

    roles = signed.get("roles")
    if not isinstance(roles, dict):
        raise MetadataError("root: roles 必须是对象")
    for role in ROLES:
        spec = roles.get(role)
        if not isinstance(spec, dict):
            raise MetadataError(f"root: 缺少角色 {role} 的配置")
        keyids = spec.get("keyids")
        threshold = spec.get("threshold")
        if not isinstance(keyids, list) or not keyids:
            raise MetadataError(f"root: 角色 {role} 的 keyids 必须是非空列表")
        if not all(isinstance(k, str) for k in keyids):
            raise MetadataError(f"root: 角色 {role} 的 keyids 必须全是字符串")
        if len(set(keyids)) != len(keyids):
            raise MetadataError(f"root: 角色 {role} 的 keyids 有重复")
        for keyid in keyids:
            if keyid not in key_pubhex:
                raise MetadataError(
                    f"root: 角色 {role} 引用了未在 keys 中定义的 keyid {keyid}"
                )
        if not isinstance(threshold, int) or isinstance(threshold, bool):
            raise MetadataError(f"root: 角色 {role} 的 threshold 必须是整数")
        if threshold < 1 or threshold > len(keyids):
            raise MetadataError(
                f"root: 角色 {role} 的 threshold 必须在 1..keyids 数量之间"
            )


def _check_targets_body(signed: dict) -> None:
    targets = signed.get("targets")
    if not isinstance(targets, dict):
        raise MetadataError("targets: targets 必须是对象")
    for name, meta in targets.items():
        if not isinstance(name, str) or not name:
            raise MetadataError("targets: 目标名必须是非空字符串")
        _require_file_meta(meta, f"targets 目标 {name}")


def _check_meta_body(signed: dict, role: str) -> None:
    meta = signed.get("meta")
    if not isinstance(meta, dict):
        raise MetadataError(f"{role}: meta 必须是对象")
    allowed = _META_KEYS[role]
    for key in allowed:
        if key not in meta:
            raise MetadataError(f"{role}: meta 缺少 {key}")
    for key, entry in meta.items():
        if key not in allowed:
            raise MetadataError(f"{role}: meta 不允许包含 {key}")
        if not isinstance(entry, dict):
            raise MetadataError(f"{role}: meta.{key} 必须是对象")
        version = entry.get("version")
        if (
            not isinstance(version, int)
            or isinstance(version, bool)
            or version <= 0
        ):
            raise MetadataError(f"{role}: meta.{key}.version 必须是正整数")
        _require_file_meta(entry, f"{role} meta.{key}")


def _require_file_meta(meta: Any, where: str) -> None:
    """长度 + 至少 sha256 的哈希绑定；忽略其它哈希算法。"""

    if not isinstance(meta, dict):
        raise MetadataError(f"{where}: 文件元数据必须是对象")
    length = meta.get("length")
    if not isinstance(length, int) or isinstance(length, bool) or length < 0:
        raise MetadataError(f"{where}: length 必须是非负整数")
    hashes = meta.get("hashes")
    if not isinstance(hashes, dict) or not hashes:
        raise MetadataError(f"{where}: hashes 必须是非空对象")
    sha = hashes.get("sha256")
    if not isinstance(sha, str) or len(sha) != 64:
        raise MetadataError(f"{where}: hashes.sha256 必须是 64 位十六进制字符串")
    try:
        bytes.fromhex(sha)
    except ValueError as exc:
        raise MetadataError(f"{where}: hashes.sha256 不是合法十六进制") from exc
    if sha != sha.lower():
        raise MetadataError(f"{where}: hashes.sha256 必须使用小写十六进制")


# ---------------------------------------------------------------------------
# 访问器
# ---------------------------------------------------------------------------


def parse_expires(value: str, role: str = "metadata") -> datetime:
    """解析 ISO 8601 过期时间，返回带 UTC 时区的 datetime。"""

    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise MetadataError(f"{role}: expires 不是合法的 ISO 8601 时间") from exc
    if dt.tzinfo is None:
        raise MetadataError(f"{role}: expires 必须带有时区（建议 UTC，结尾 Z）")
    return dt.astimezone(timezone.utc)


def is_expired(signed: dict, now: datetime) -> bool:
    if now.tzinfo is None:
        raise ValueError("now 必须带有时区信息")
    return now >= parse_expires(signed["expires"])


def file_meta(meta: dict) -> FileMeta:
    return FileMeta(length=meta["length"], sha256=meta["hashes"]["sha256"])


def target_map(signed: dict) -> dict[str, FileMeta]:
    return {name: file_meta(meta) for name, meta in signed["targets"].items()}


def role_keys(signed_root: dict, role: str) -> tuple[list[str], int]:
    """返回 root 中某角色的 (keyids, threshold)。"""

    spec = signed_root["roles"][role]
    return list(spec["keyids"]), spec["threshold"]


def root_public_hex(signed_root: dict, keyid: str) -> str | None:
    """返回 root 公钥库中 keyid 对应的 ed25519 公钥（hex）。"""

    keyobj = signed_root.get("keys", {}).get(keyid)
    return None if keyobj is None else keyobj["keyval"]["public"]


def root_keys_section(signers: Iterable[KeyPair]) -> dict:
    """构建 root 的 ``keys`` 段：keyid -> 公钥对象。"""

    return {
        signer.keyid(): {
            "keytype": KEYTYPE,
            "scheme": SCHEME,
            "keyval": {"public": signer.public_hex()},
        }
        for signer in signers
    }


def signed_bytes(metadata: Metadata) -> bytes:
    """返回被签名的规范化字节串。"""

    return canonical_mod.canonical(metadata.signed)


# ---------------------------------------------------------------------------
# 构建辅助（供 repo_tool / 测试使用；服务端本身只验证、不签发）
# ---------------------------------------------------------------------------


def make_signed(
    role: str,
    version: int,
    expires: datetime | str,
    extra: dict[str, Any] | None = None,
) -> dict:
    body: dict[str, Any] = {
        "_type": role,
        "version": version,
        "expires": _format_expires(expires),
    }
    if extra:
        body.update(extra)
    return body


def _format_expires(expires: datetime | str) -> str:
    if isinstance(expires, str):
        # 提前解析一次，保证调用方拿到的一定是合法时间
        parse_expires(expires)
        return expires
    if expires.tzinfo is None:
        raise MetadataError("expires 必须带有时区")
    return expires.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_envelope(signed: dict, signers: Iterable[KeyPair]) -> bytes:
    """用给定私钥对负载签名，返回完整元数据 JSON 字节串。

    签名按 keyid 排序，保证同一输入得到字节级确定的输出。
    """

    payload = canonical_mod.canonical(signed)
    signatures = sorted(
        (
            {"keyid": signer.keyid(), "sig": signer.sign(payload)}
            for signer in signers
        ),
        key=lambda item: item["keyid"],
    )
    return json.dumps(
        {"signatures": signatures, "signed": signed},
        separators=(",", ":"),
        ensure_ascii=False,
        sort_keys=False,
    ).encode("utf-8")


def root_role_spec(keys: Iterable[KeyPair | str], threshold: int) -> dict:
    """生成 root 里单个角色的配置，接受 KeyPair 或公钥十六进制。"""

    keyids = [
        k.keyid() if isinstance(k, KeyPair) else keyid_for_public_hex(k)
        for k in keys
    ]
    return {"keyids": keyids, "threshold": threshold}
