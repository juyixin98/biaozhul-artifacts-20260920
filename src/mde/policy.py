"""策略存储：不可变版本化 + 乐观并发控制。

核心约束
========
* 策略按 ``policy_id`` 组织，每次 :meth:`publish` 产生版本号 +1 的
  **不可变**新版本；旧版本内容永不修改。
* 导出任务固定到某个具体版本（service 层快照），因此“策略更新竞争”
  不会改变进行中/已完成导出的决策依据。
* 发布支持 ``expected_version`` 乐观锁：两个客户端基于同一旧版本起草
  更新时，第二个发布会得到 :class:`PolicyConflict`，调用方必须重新读取
  并合并，而不是静默覆盖。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from abc import ABC, abstractmethod
from typing import Any, Iterable

from .canonical import stable_json_dumps
from .errors import PolicyConflict, PolicyNotFound, PolicyValidationError
from .models import Policy, Rule, now_iso


def build_policy(policy_id: str, version: int, rules: Iterable[Rule | dict],
                 aliases: Iterable = (), default_action: str = "deny",
                 description: str = "", created_at: str | None = None) -> Policy:
    """从 Rule 对象或 dict 构造不可变 Policy（dict 形式便于从 JSON 加载）。"""
    rs: list[Rule] = []
    for r in rules:
        rs.append(r if isinstance(r, Rule) else Rule.from_dict(r))
    return Policy(
        policy_id=policy_id,
        version=version,
        rules=tuple(rs),
        aliases=_normalize_aliases(aliases),
        default_action=default_action,
        created_at=created_at or now_iso(),
        description=description,
    )


def _normalize_aliases(aliases: Iterable) -> tuple[tuple[str, tuple[str, ...]], ...]:
    out: list[tuple[str, tuple[str, ...]]] = []
    for item in aliases:
        if isinstance(item, dict):
            c = item.get("canonical")
            al = item.get("aliases", [])
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            c, al = item
        else:
            raise PolicyValidationError(f"bad alias entry: {item!r}")
        if not isinstance(al, list) or not all(isinstance(x, str) for x in al):
            raise PolicyValidationError(f"alias list for {c!r} must be strings")
        out.append((str(c), tuple(al)))
    return tuple(out)


class PolicyStore(ABC):
    """策略存储抽象接口。"""

    @abstractmethod
    def publish(self, policy_id: str, rules: Iterable[Rule | dict],
                *, aliases: Iterable = (), default_action: str = "deny",
                description: str = "",
                expected_version: int | None = None) -> Policy:
        """发布新版本。

        expected_version 给定时，仅当当前版本恰为该值才成功，
        否则抛 :class:`PolicyConflict`。
        """

    @abstractmethod
    def get(self, policy_id: str, version: int | None = None) -> Policy:
        """取指定版本；version=None 取最新。不存在抛 PolicyNotFound。"""

    @abstractmethod
    def list_versions(self, policy_id: str) -> list[int]:
        """按版本号升序返回。"""

    @abstractmethod
    def list_policies(self) -> list[str]:
        """返回所有 policy_id。"""


class InMemoryPolicyStore(PolicyStore):
    """线程安全的内存存储（测试与默认服务使用）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._data: dict[str, dict[int, Policy]] = {}

    def publish(self, policy_id, rules, *, aliases=(), default_action="deny",
                description="", expected_version=None) -> Policy:
        with self._lock:
            versions = self._data.setdefault(policy_id, {})
            current = max(versions) if versions else 0
            if expected_version is not None and expected_version != current:
                raise PolicyConflict(policy_id, expected_version, current)
            new_version = current + 1
            policy = build_policy(policy_id, new_version, rules,
                                  aliases=aliases,
                                  default_action=default_action,
                                  description=description)
            versions[new_version] = policy
            return policy

    def get(self, policy_id, version=None) -> Policy:
        with self._lock:
            versions = self._data.get(policy_id)
            if not versions:
                raise PolicyNotFound(f"no such policy: {policy_id!r}")
            if version is None:
                return versions[max(versions)]
            if version not in versions:
                raise PolicyNotFound(
                    f"policy {policy_id!r} has no version {version} "
                    f"(have {sorted(versions)})")
            return versions[version]

    def list_versions(self, policy_id) -> list[int]:
        with self._lock:
            versions = self._data.get(policy_id)
            return sorted(versions) if versions else []

    def list_policies(self) -> list[str]:
        with self._lock:
            return sorted(self._data)

    # ---- 测试/导入辅助 ----

    def load_dict(self, doc: dict[str, Any]) -> Policy:
        """直接载入一个完整版本定义（不触发版本自增），用于从文件恢复。"""
        policy = Policy.from_dict(doc)
        with self._lock:
            versions = self._data.setdefault(policy.policy_id, {})
            if policy.version in versions:
                raise PolicyConflict(policy.policy_id, policy.version,
                                     policy.version)
            versions[policy.version] = policy
        return policy


class FilePolicyStore(PolicyStore):
    """以 JSON 文件持久化的存储：<dir>/<policy_id>.json 保存全部版本。

    文件写入采用临时文件 + os.replace 原子替换；进程内用锁串行化。
    跨进程并发不在本项目范围内（纯本地服务）。
    """

    def __init__(self, directory: str) -> None:
        self._dir = directory
        os.makedirs(directory, exist_ok=True)
        self._lock = threading.RLock()
        self._cache: dict[str, dict[int, Policy]] = {}

    def _path(self, policy_id: str) -> str:
        safe = all(c.isalnum() or c in "-_." for c in policy_id)
        if not policy_id or not safe or policy_id in (".", ".."):
            raise PolicyValidationError(
                f"unsafe policy_id for file storage: {policy_id!r} "
                "(allowed: letters, digits, '-', '_', '.')")
        return os.path.join(self._dir, f"{policy_id}.json")

    def _read(self, policy_id: str) -> dict[int, Policy]:
        if policy_id in self._cache:
            return self._cache[policy_id]
        path = self._path(policy_id)
        versions: dict[int, Policy] = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                doc = json.load(f)
            for vdoc in doc.get("versions", []):
                p = Policy.from_dict(vdoc)
                versions[p.version] = p
        self._cache[policy_id] = versions
        return versions

    def _write(self, policy_id: str, versions: dict[int, Policy]) -> None:
        doc = {
            "policy_id": policy_id,
            "versions": [versions[v].to_dict()
                         for v in sorted(versions)],
        }
        path = self._path(policy_id)
        fd, tmp = tempfile.mkstemp(prefix=".policy-", suffix=".tmp",
                                   dir=self._dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(stable_json_dumps(doc))
                f.write("\n")
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def publish(self, policy_id, rules, *, aliases=(), default_action="deny",
                description="", expected_version=None) -> Policy:
        with self._lock:
            versions = self._read(policy_id)
            current = max(versions) if versions else 0
            if expected_version is not None and expected_version != current:
                raise PolicyConflict(policy_id, expected_version, current)
            new_version = current + 1
            policy = build_policy(policy_id, new_version, rules,
                                  aliases=aliases,
                                  default_action=default_action,
                                  description=description)
            versions = dict(versions)
            versions[new_version] = policy
            self._write(policy_id, versions)
            self._cache[policy_id] = versions
            return policy

    def get(self, policy_id, version=None) -> Policy:
        with self._lock:
            versions = self._read(policy_id)
            if not versions:
                raise PolicyNotFound(f"no such policy: {policy_id!r}")
            if version is None:
                return versions[max(versions)]
            if version not in versions:
                raise PolicyNotFound(
                    f"policy {policy_id!r} has no version {version}")
            return versions[version]

    def list_versions(self, policy_id) -> list[int]:
        with self._lock:
            return sorted(self._read(policy_id))

    def load_dict(self, doc: dict[str, Any]) -> Policy:
        """导入一个完整版本定义；已存在的版本不可覆盖（不可变）。"""
        policy = Policy.from_dict(doc)
        with self._lock:
            versions = dict(self._read(policy.policy_id))
            if policy.version in versions:
                raise PolicyConflict(policy.policy_id, policy.version,
                                     policy.version)
            versions[policy.version] = policy
            self._write(policy.policy_id, versions)
            self._cache[policy.policy_id] = versions
        return policy

    def list_policies(self) -> list[str]:
        out = []
        for name in sorted(os.listdir(self._dir)):
            if name.endswith(".json"):
                out.append(name[:-5])
        return out
