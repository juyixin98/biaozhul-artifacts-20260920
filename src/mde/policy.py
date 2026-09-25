"""导出策略：校验、编译视图、不可变快照与策略仓库。

策略文档结构见 examples/policy.json 与 README。关键安全性质：
- 发布后的策略**不可变**：以规范化内容的 SHA256 指纹为标识，文件只增不改；
- 导出任务固定到具体指纹，策略更新不影响进行中/已完成的任务（无 TOCTOU）；
- 同一用途内任意两条规则不得存在路径前缀包含关系 —— 否则发布即拒绝，
  从而不存在"父允许、子拒绝"这类语义含糊的组合，嵌套结构无法钻空子；
- 别名只是路径重命名：必须指向已有规则，禁止别名链，别名源不得与真实字段路径冲突。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, FrozenSet, List, Mapping, Tuple

from . import POLICY_SCHEMA
from .paths import PathError, encode_segment, join_tokens, split_path
from .signing import fingerprint as fp_of
from .transforms import TransformError, validate_transform

ALLOW = "allow"
DENY = "deny"
GENERALIZE = "generalize"
ACTIONS = (ALLOW, DENY, GENERALIZE)

Tokens = Tuple[str, ...]


class PolicyError(ValueError):
    """策略文档非法。"""


def freeze(obj: Any) -> Any:
    """递归冻结：dict -> 只读映射，list -> tuple。"""
    if isinstance(obj, Mapping):
        return MappingProxyType({k: freeze(v) for k, v in obj.items()})
    if isinstance(obj, (list, tuple)):
        return tuple(freeze(v) for v in obj)
    return obj


def to_plain(obj: Any) -> Any:
    """冻结对象转回可 JSON 序列化的普通结构。"""
    if isinstance(obj, Mapping):
        return {k: to_plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_plain(v) for v in obj]
    return obj


@dataclass(frozen=True)
class TransformRule:
    name: str
    params: Any  # 冻结后的参数


@dataclass(frozen=True)
class CompiledPurpose:
    name: str
    default: str
    allow: FrozenSet[Tokens]
    deny: FrozenSet[Tokens]
    generalize: Mapping[Tokens, TransformRule]

    def all_paths(self) -> FrozenSet[Tokens]:
        return frozenset(self.allow) | frozenset(self.deny) | frozenset(self.generalize)


@dataclass(frozen=True)
class CompiledPolicy:
    doc: Any  # 冻结后的原始策略文档
    policy_fingerprint: str
    purposes: Mapping[str, CompiledPurpose]
    aliases: Mapping[Tokens, Tokens]

    def purpose(self, name: str) -> CompiledPurpose:
        if name not in self.purposes:
            raise PolicyError(
                f"策略中不存在用途 {name!r}，可用: {sorted(self.purposes)}"
            )
        return self.purposes[name]


# ---- 校验 ------------------------------------------------------------------

def _require_str(obj: Any, key: str, where: str) -> str:
    v = obj.get(key)
    if not isinstance(v, str) or not v:
        raise PolicyError(f"{where}: {key} 必须是非空字符串")
    return v


def _no_extra_keys(obj: Mapping, allowed: set, where: str) -> None:
    extra = set(obj) - allowed
    if extra:
        raise PolicyError(f"{where}: 含未知键 {sorted(extra)}")


def _validate_purpose(name: str, spec: Any) -> CompiledPurpose:
    if not isinstance(name, str) or not name:
        raise PolicyError("用途名必须是非空字符串")
    if not isinstance(spec, Mapping):
        raise PolicyError(f"用途 {name!r} 的定义必须是对象")
    _no_extra_keys(spec, {"allow", "deny", "generalize", "default"}, f"用途 {name!r}")

    default = spec.get("default", DENY)
    if default not in (ALLOW, DENY):
        raise PolicyError(f"用途 {name!r}: default 只能是 allow|deny")

    def paths(key: str) -> list[Tokens]:
        vals = spec.get(key, [])
        if not isinstance(vals, list) or not all(isinstance(v, str) for v in vals):
            raise PolicyError(f"用途 {name!r}: {key} 必须是字符串数组")
        out: list[Tokens] = []
        for p in vals:
            try:
                out.append(tuple(split_path(p)))
            except PathError as e:
                raise PolicyError(f"用途 {name!r}: {e}") from e
        return out

    allow = paths("allow")
    deny = paths("deny")
    gen_spec = spec.get("generalize", {})
    if not isinstance(gen_spec, Mapping) or not all(
        isinstance(k, str) for k in gen_spec
    ):
        raise PolicyError(f"用途 {name!r}: generalize 必须是 路径->定义 的对象")

    generalize: dict[Tokens, TransformRule] = {}
    for path_str, tspec in gen_spec.items():
        try:
            tokens = tuple(split_path(path_str))
        except PathError as e:
            raise PolicyError(f"用途 {name!r}: {e}") from e
        if not isinstance(tspec, Mapping):
            raise PolicyError(f"用途 {name!r}: {path_str} 的泛化定义必须是对象")
        _no_extra_keys(tspec, {"transform", "params"}, f"用途 {name!r}: {path_str}")
        tname = _require_str(tspec, "transform", f"用途 {name!r}: {path_str}")
        raw_params = tspec.get("params", {})
        try:
            params = validate_transform(tname, raw_params)
        except TransformError as e:
            raise PolicyError(f"用途 {name!r}: {path_str}: {e}") from e
        generalize[tokens] = TransformRule(tname, freeze(params))

    # 同一用途内：路径不得重复、不得互相为前缀（消除一切父子歧义）
    seen: dict[Tokens, str] = {}

    def _check_unique(tokens: Tokens, action: str) -> None:
        if tokens in seen:
            raise PolicyError(
                f"用途 {name!r}: 路径 {join_tokens(tokens)!r} 同时出现在 "
                f"{seen[tokens]} 与 {action} 中"
            )
        for other in seen:
            if other == tokens[: len(other)] or tokens == other[: len(tokens)]:
                raise PolicyError(
                    f"用途 {name!r}: 路径 {join_tokens(tokens)!r} 与 "
                    f"{join_tokens(other)!r} 存在前缀包含关系；"
                    f"本版本要求同一用途内规则互不为前缀，请细化规则"
                )
        seen[tokens] = action

    for t in allow:
        _check_unique(t, ALLOW)
    for t in deny:
        _check_unique(t, DENY)
    for t in generalize:
        _check_unique(t, GENERALIZE)

    return CompiledPurpose(
        name=name,
        default=default,
        allow=frozenset(allow),
        deny=frozenset(deny),
        generalize=MappingProxyType(generalize),
    )


def validate_policy(doc: Any) -> CompiledPolicy:
    """完整校验策略文档并返回编译视图。"""
    if not isinstance(doc, Mapping):
        raise PolicyError("策略文档必须是 JSON 对象")
    _no_extra_keys(
        doc,
        {"version", "policy_id", "revision", "description", "purposes", "aliases"},
        "策略根",
    )
    if doc.get("version") != POLICY_SCHEMA:
        raise PolicyError(f"version 必须是 {POLICY_SCHEMA!r}")
    _require_str(doc, "policy_id", "策略根")
    revision = doc.get("revision", 1)
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise PolicyError("revision 必须是 >=1 的整数")
    desc = doc.get("description", "")
    if not isinstance(desc, str):
        raise PolicyError("description 必须是字符串")

    purposes_spec = doc.get("purposes")
    if not isinstance(purposes_spec, Mapping) or not purposes_spec:
        raise PolicyError("purposes 必须是非空对象")
    purposes: dict[str, CompiledPurpose] = {}
    for name, spec in purposes_spec.items():
        if name in purposes:
            raise PolicyError(f"用途 {name!r} 重复")
        purposes[name] = _validate_purpose(name, spec)

    # 别名：源 -> 已存在的精确规则路径；禁止链、禁止与真实字段撞路径
    aliases_spec = doc.get("aliases", {})
    if not isinstance(aliases_spec, Mapping) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in aliases_spec.items()
    ):
        raise PolicyError("aliases 必须是 路径字符串->路径字符串 的对象")
    all_rule_paths = frozenset().union(*(p.all_paths() for p in purposes.values()))
    aliases: dict[Tokens, Tokens] = {}
    for src, dst in aliases_spec.items():
        try:
            src_t = tuple(split_path(src))
            dst_t = tuple(split_path(dst))
        except PathError as e:
            raise PolicyError(f"别名 {src!r}: {e}") from e
        if src_t in all_rule_paths:
            raise PolicyError(f"别名源 {src!r} 与真实规则字段冲突，别名不得遮蔽字段")
        if dst_t not in all_rule_paths:
            raise PolicyError(
                f"别名 {src!r} -> {dst!r}：目标必须是某用途中已存在的精确规则字段"
            )
        if src_t in aliases:
            raise PolicyError(f"别名源 {src!r} 重复")
        aliases[src_t] = dst_t
    # 别名链：任何别名源不得等于另一个别名目标，任何目标也不得是别名源
    sources = set(aliases)
    for dst in aliases.values():
        if dst in sources:
            raise PolicyError(f"存在别名链：{join_tokens(dst)!r} 同时是另一个别名的源")

    frozen_doc = freeze(doc)
    return CompiledPolicy(
        doc=frozen_doc,
        policy_fingerprint=fp_of(to_plain(frozen_doc)),
        purposes=MappingProxyType(purposes),
        aliases=MappingProxyType(aliases),
    )


def encode_field_path(segments: List[str]) -> str:
    """供外部把原始键序列构造成策略路径。"""
    return join_tokens([encode_segment(s) if s != "[]" else "[]" for s in segments])


# ---- 不可变策略仓库 --------------------------------------------------------

@dataclass(frozen=True)
class StoredPolicy:
    policy: CompiledPolicy
    created_at: str  # UTC ISO8601


class PolicyStore:
    """基于文件目录的只增策略仓库；内存带缓存，读写加锁。

    目录布局::

        base/policies/<fingerprint>.json   # 不可变策略信封
        base/index.json                    # policy_id -> {revision: fingerprint}
    """

    def __init__(self, base: Path):
        self.base = Path(base)
        self.policy_dir = self.base / "policies"
        self.index_path = self.base / "index.json"
        self.policy_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._cache: Dict[str, StoredPolicy] = {}
        if not self.index_path.exists():
            self._write_index({})

    # 索引
    def _read_index(self) -> Dict[str, Dict[str, str]]:
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            raise PolicyError(f"策略索引损坏: {self.index_path}: {e}") from e
        if not isinstance(data, dict):
            raise PolicyError("策略索引格式损坏")
        return data

    def _write_index(self, data: dict) -> None:
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.index_path)

    def publish(self, doc: Any, created_at: str) -> StoredPolicy:
        """校验并发布；指纹已存在则幂等返回。created_at 由调用方注入（便于测试）。"""
        compiled = validate_policy(doc)
        fp = compiled.policy_fingerprint
        with self._lock:
            if fp in self._cache:
                return self._cache[fp]
            path = self.policy_dir / f"{fp}.json"
            envelope = {
                "version": "mde/stored-policy@v1",
                "created_at": created_at,
                "policy": to_plain(compiled.doc),
                "policy_fingerprint": fp,
            }
            if not path.exists():
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(envelope, indent=2), encoding="utf-8")
                tmp.replace(path)

            index = self._read_index()
            pid = doc["policy_id"]
            entry = index.setdefault(pid, {})
            revision = str(doc["revision"])
            if revision in entry and entry[revision] != fp:
                raise PolicyError(
                    f"策略 {pid} revision={revision} 已发布为另一内容（不可变冲突）"
                )
            if revision not in entry:
                entry[revision] = fp
                self._write_index(index)

            stored = StoredPolicy(policy=compiled, created_at=created_at)
            self._cache[fp] = stored
            return stored

    def get(self, fingerprint: str) -> StoredPolicy:
        """按指纹取策略。文件是真相来源：每次都读盘并复核指纹，
        内存缓存只避免重复解析，绝不绕过磁盘完整性检查。"""
        with self._lock:
            path = self.policy_dir / f"{fingerprint}.json"
            if not path.exists():
                raise PolicyError(f"未知策略指纹: {fingerprint}")
            envelope = json.loads(path.read_text(encoding="utf-8"))
            stored_fp = envelope.get("policy_fingerprint")
            compiled = validate_policy(envelope["policy"])
            if compiled.policy_fingerprint != fingerprint or stored_fp != fingerprint:
                raise PolicyError(f"策略文件与指纹不符（可能已被篡改）: {fingerprint}")
            stored = StoredPolicy(policy=compiled, created_at=envelope["created_at"])
            self._cache[fingerprint] = stored
            return stored

    def latest(self, policy_id: str) -> StoredPolicy:
        with self._lock:
            entry = self._read_index().get(policy_id)
            if not entry:
                raise PolicyError(f"未知策略: {policy_id!r}")
            revision = max(entry, key=lambda r: int(r))
            return self.get(entry[revision])

    def revisions(self, policy_id: str) -> List[Tuple[int, str]]:
        entry = self._read_index().get(policy_id, {})
        return sorted(((int(r), fp) for r, fp in entry.items()), key=lambda x: x[0])
