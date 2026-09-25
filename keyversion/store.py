"""本地持久化层：KEK 文件、密钥版本状态、哈希链审计日志。

持久化布局（data_dir 权限 0700）::

    data_dir/
      kek.key          # 32 字节随机 KEK，0600
      state.json.tmp   # 原子写临时文件（替换 state.json 后删除）
      state.json       # 全部版本元数据 + 被包装 DEK，0600
      audit.log        # 仅追加 JSONL，哈希链，0600

崩溃一致性：状态转换与审计记录在同一把文件锁内，按
「状态文件 fsync+原子替换 → 审计条目 fsync 追加」顺序提交。
崩溃可能落在两步之间：状态已更新而最后一审计条目丢失（仅丢失审计，
不破坏状态机）；或什么都没提交。状态文件本身始终是旧版本或新版本，
不会出现半截 JSON（rename 原子性 + fsync）。
"""

from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import hashlib
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

from . import crypto
from .errors import (
    AuditLogTampered,
    DecryptVersionDestroyed,
    InvalidStateTransition,
    VersionNotFound,
)

#: 版本状态机的合法状态
GENERATED = "generated"  # 已生成、未激活：不能加密
ACTIVE = "active"  # 当前加密版本：可加密可解密
RETIRED = "retired"  # 已停用：不能再加密，仍可解密历史数据
DESTROYED = "destroyed"  # 已销毁：密钥材料删除，不可恢复、不可解密

VALID_STATES = (GENERATED, ACTIVE, RETIRED, DESTROYED)

#: 合法状态转换表
_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    GENERATED: frozenset({ACTIVE, DESTROYED}),
    ACTIVE: frozenset({RETIRED, DESTROYED}),
    RETIRED: frozenset({DESTROYED}),
    DESTROYED: frozenset(),
}

STATE_VERSION = 1


class _SharedLock:
    """按数据目录共享的排他文件锁（进程内引用计数，跨进程 flock 互斥）。"""

    def __init__(self, lock_path: Path):
        self._fp = open(lock_path, "a+b")
        self._refs = 0
        self._guard = threading.Lock()

    def acquire(self) -> None:
        with self._guard:
            if self._refs == 0:
                fcntl.flock(self._fp.fileno(), fcntl.LOCK_EX)
            self._refs += 1

    def release(self) -> bool:
        """返回 True 表示最后一个引用已释放（调用方应从注册表移除）。"""
        with self._guard:
            self._refs -= 1
            if self._refs == 0:
                fcntl.flock(self._fp.fileno(), fcntl.LOCK_UN)
                self._fp.close()
                return True
            return False


@dataclasses.dataclass(frozen=True)
class AuditEntry:
    """一条审计记录。绝不包含明文、DEK、被包装 DEK或 KEK。"""

    seq: int
    ts: str
    action: str
    version_id: str
    old_state: str | None
    new_state: str | None
    result: str
    request_id: str | None
    detail: dict[str, Any]
    prev_hash: str
    entry_hash: str

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


class KeyStore:
    """密钥版本存储。所有公开操作都在进程内锁 + 文件锁内串行执行。"""

    #: 进程级文件锁注册表：同一 data_dir 的多个 KeyStore 实例共享一把 flock，
    #: 引用计数。不同进程之间仍由 flock(LOCK_EX) 互斥。
    _file_locks: dict[str, "_SharedLock"] = {}
    _file_locks_guard = threading.Lock()

    def __init__(self, data_dir: str | os.PathLike[str]):
        self.data_dir = Path(data_dir)
        self._kek_path = self.data_dir / "kek.key"
        self._state_path = self.data_dir / "state.json"
        self._tmp_path = self.data_dir / "state.json.tmp"
        self._audit_path = self.data_dir / "audit.log"
        self._lock_path = self.data_dir / ".lock"
        self._thread_lock = threading.RLock()
        self._shared_lock: "_SharedLock | None" = None
        self._lock_fp = None
        self._kek: bytes | None = None
        self._state: dict[str, Any] | None = None
        self._audit_seq: int = 0
        self._prev_hash: str = ""
        self._opened = False

    # ------------------------------------------------------------------
    # 打开 / 初始化
    # ------------------------------------------------------------------
    def open(self) -> "KeyStore":
        """创建或打开本地密钥库，加载 KEK、状态并校验审计链。"""
        with self._thread_lock:
            os.makedirs(self.data_dir, mode=0o700, exist_ok=True)
            try:
                os.chmod(self.data_dir, 0o700)
            except OSError:
                pass
            key = str(self.data_dir.resolve())
            with self._file_locks_guard:
                shared = self._file_locks.get(key)
                if shared is None:
                    shared = _SharedLock(self._lock_path)
                    self._file_locks[key] = shared
                shared.acquire()
                self._shared_lock = shared
            try:
                self._kek = self._load_or_create_kek()
                if self._state_path.exists():
                    self._state = self._read_state()
                    self._load_and_verify_audit()
                    self._verify_state_integrity()
                else:
                    self._state = self._empty_state()
                    self._audit_seq = 0
                    self._prev_hash = ""
                    self._write_state_locked()
                self._opened = True
            except BaseException:
                self._release_shared_lock(key)
                raise
        return self

    def _release_shared_lock(self, key: str) -> None:
        with self._file_locks_guard:
            if self._shared_lock is not None:
                last = self._shared_lock.release()
                if last:
                    self._file_locks.pop(key, None)
                self._shared_lock = None

    def close(self) -> None:
        if not self._opened and self._shared_lock is None:
            return
        with self._thread_lock:
            key = str(self.data_dir.resolve())
            self._release_shared_lock(key)
            self._kek = None
            self._state = None
            self._opened = False

    def __enter__(self) -> "KeyStore":
        return self.open()

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _load_or_create_kek(self) -> bytes:
        if self._kek_path.exists():
            kek = self._kek_path.read_bytes()
            if len(kek) != crypto.KEY_LEN:
                raise AuditLogTampered("KEK file corrupted: unexpected length")
            return kek
        kek = os.urandom(crypto.KEY_LEN)
        self._atomic_write_bytes(self._kek_path, kek, mode=0o600)
        return kek

    def _empty_state(self) -> dict[str, Any]:
        return {
            "state_version": STATE_VERSION,
            "active_version_id": None,
            "versions": {},
        }

    # ------------------------------------------------------------------
    # 状态文件读写（原子替换 + fsync）
    # ------------------------------------------------------------------
    def _read_state(self) -> dict[str, Any]:
        try:
            raw = self._state_path.read_bytes()
            state = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AuditLogTampered(f"state file unreadable: {exc}") from exc
        if not isinstance(state, dict) or state.get("state_version") != STATE_VERSION:
            raise AuditLogTampered("state file has unsupported state_version")
        if not isinstance(state.get("versions"), dict):
            raise AuditLogTampered("state file missing versions map")
        return state

    def _write_state_locked(self) -> None:
        """在持锁状态下原子写状态文件：tmp -> fsync -> rename -> fsync 目录。"""
        payload = json.dumps(self._state, indent=2, sort_keys=True).encode("utf-8")
        fd = os.open(self._tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(self._tmp_path, 0o600)
        os.replace(self._tmp_path, self._state_path)
        self._fsync_dir()

    def _fsync_dir(self) -> None:
        try:
            dfd = os.open(self.data_dir, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            # 某些文件系统不支持目录 fsync，rename 已尽量保证原子可见
            pass

    @staticmethod
    def _atomic_write_bytes(path: Path, data: bytes, mode: int = 0o600) -> None:
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(tmp, mode)
        os.replace(tmp, path)

    # ------------------------------------------------------------------
    # 审计日志（JSONL + SHA-256 哈希链）
    # ------------------------------------------------------------------
    def _chain_payload(self, entry: dict[str, Any]) -> bytes:
        """参与哈希的规范化内容（不含 entry_hash 自身）。"""
        signed = {k: v for k, v in entry.items() if k != "entry_hash"}
        return json.dumps(signed, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def _load_and_verify_audit(self) -> None:
        """读取审计日志并验证哈希链；容忍末尾被截断的半行（崩溃可能造成）。"""
        self._audit_seq = 0
        self._prev_hash = ""
        if not self._audit_path.exists():
            return
        good_bytes = b""
        with open(self._audit_path, "rb") as fp:
            for raw_line in fp:
                if not raw_line.endswith(b"\n"):
                    # 末尾不完整行：崩溃可能写了一半，截掉它
                    break
                line = raw_line.strip()
                if not line:
                    good_bytes += raw_line
                    continue
                try:
                    entry = json.loads(line.decode("utf-8"))
                    payload = self._chain_payload(entry)
                    expect = hashlib.sha256(self._prev_hash.encode() + payload).hexdigest()
                except (UnicodeDecodeError, json.JSONDecodeError):
                    break
                if not isinstance(entry, dict) or entry.get("entry_hash") != expect:
                    raise AuditLogTampered(
                        f"audit hash chain broken at seq={entry.get('seq')!r}"
                    )
                self._audit_seq = int(entry["seq"])
                self._prev_hash = entry["entry_hash"]
                good_bytes += raw_line
        # 截掉末尾半行，保证后续 append 不会拼在半行后面
        if self._audit_path.read_bytes() != good_bytes:
            self._atomic_write_bytes(self._audit_path, good_bytes, mode=0o600)

    def _append_audit_locked(
        self,
        action: str,
        version_id: str,
        old_state: str | None,
        new_state: str | None,
        result: str,
        detail: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> AuditEntry:
        self._audit_seq += 1
        entry: dict[str, Any] = {
            "seq": self._audit_seq,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "action": action,
            "version_id": version_id,
            "old_state": old_state,
            "new_state": new_state,
            "result": result,
            "request_id": request_id,
            "detail": detail or {},
            "prev_hash": self._prev_hash,
        }
        entry["entry_hash"] = hashlib.sha256(
            self._prev_hash.encode() + self._chain_payload(entry)
        ).hexdigest()
        line = json.dumps(entry, sort_keys=True, separators=(",", ":")).encode("utf-8")
        with open(self._audit_path, "ab") as fp:
            fp.write(line + b"\n")
            fp.flush()
            os.fsync(fp.fileno())
        self._prev_hash = entry["entry_hash"]
        return AuditEntry(**entry)

    def list_audit(self) -> list[AuditEntry]:
        """返回全部审计条目（重新从磁盘读取并校验哈希链）。"""
        with self._thread_lock:
            self._load_and_verify_audit()
            entries: list[AuditEntry] = []
            if self._audit_path.exists():
                for raw_line in self._audit_path.read_bytes().splitlines():
                    line = raw_line.strip()
                    if line:
                        entries.append(AuditEntry(**json.loads(line.decode("utf-8"))))
            return entries

    # ------------------------------------------------------------------
    # 状态完整性校验（启动时）
    # ------------------------------------------------------------------
    def _verify_state_integrity(self) -> None:
        assert self._state is not None
        versions = self._state["versions"]
        active_id = self._state["active_version_id"]
        active_count = 0
        for vid, rec in versions.items():
            st = rec.get("state")
            if st not in VALID_STATES:
                raise AuditLogTampered(f"version {vid} has unknown state {st!r}")
            if st == ACTIVE:
                active_count += 1
                if vid != active_id:
                    raise AuditLogTampered(f"active version {vid} not reflected in pointer")
            if st == DESTROYED:
                if rec.get("wrapped_dek") is not None:
                    raise AuditLogTampered(f"destroyed version {vid} still has key material")
            else:
                blob_b64 = rec.get("wrapped_dek")
                if not isinstance(blob_b64, str):
                    raise AuditLogTampered(f"version {vid} missing wrapped key")
                # 用 KEK 解开并核对指纹，发现磁盘被替换/损坏立即拒绝启动
                import base64

                dek = crypto.unwrap_dek(self._kek, base64.b64decode(blob_b64))
                if crypto.key_fingerprint(dek) != rec.get("fingerprint"):
                    raise AuditLogTampered(f"version {vid} fingerprint mismatch")
        if active_id is not None and versions.get(active_id, {}).get("state") != ACTIVE:
            raise AuditLogTampered("active pointer points to a non-active version")
        if active_count > 1:
            raise AuditLogTampered("more than one active version in state")

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------
    def _record(self, version_id: str) -> dict[str, Any]:
        assert self._state is not None
        rec = self._state["versions"].get(version_id)
        if rec is None:
            raise VersionNotFound(f"version {version_id!r} not found")
        return rec

    def _commit_locked(self, entry_kwargs: dict[str, Any]) -> AuditEntry:
        """先提交状态文件，再追加审计记录（均在锁内、均 fsync）。"""
        self._write_state_locked()
        return self._append_audit_locked(**entry_kwargs)

    def _new_version_locked(self) -> tuple[str, dict[str, Any], bytes]:
        assert self._state is not None and self._kek is not None
        import base64

        seq = len(self._state["versions"]) + 1
        version_id = f"v{seq:04d}-{uuid.uuid4().hex[:8]}"
        dek = crypto.generate_key()
        rec = {
            "version": seq,
            "created_ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "state": GENERATED,
            "fingerprint": crypto.key_fingerprint(dek),
            "wrapped_dek": base64.b64encode(crypto.wrap_dek(self._kek, dek)).decode("ascii"),
        }
        self._state["versions"][version_id] = rec
        return version_id, rec, dek

    # ------------------------------------------------------------------
    # 状态机操作
    # ------------------------------------------------------------------
    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        """供 service 层在同一把锁内完成“检查状态 + 加解密 + 审计”。"""
        with self._thread_lock:
            yield

    def generate(self, request_id: str | None = None) -> tuple[str, AuditEntry]:
        """生成新版本（generated 状态，不影响当前 active 版本）。"""
        with self._thread_lock:
            version_id, _rec, _dek = self._new_version_locked()
            entry = self._commit_locked(
                dict(
                    action="generate",
                    version_id=version_id,
                    old_state=None,
                    new_state=GENERATED,
                    result="success",
                    detail={},
                    request_id=request_id,
                )
            )
            return version_id, entry

    def rotate(self, request_id: str | None = None) -> tuple[str, AuditEntry]:
        """生成新版本并激活；若已有 active 版本则将其转为 retired。

        整个转换在一次状态提交内完成，崩溃后只会看到「轮换前」或「轮换后」，
        不会出现两个 active 版本。
        """
        with self._thread_lock:
            assert self._state is not None
            old_active = self._state["active_version_id"]
            entries: list[dict[str, Any]] = []
            if old_active is not None:
                old_rec = self._record(old_active)
                if old_rec["state"] != ACTIVE:
                    raise AuditLogTampered("active pointer inconsistent during rotate")
                old_rec["state"] = RETIRED
                entries.append(
                    dict(
                        action="deactivate",
                        version_id=old_active,
                        old_state=ACTIVE,
                        new_state=RETIRED,
                        result="success",
                        detail={"reason": "rotate"},
                    )
                )
            new_id, _rec, _dek = self._new_version_locked()
            self._state["versions"][new_id]["state"] = ACTIVE
            self._state["active_version_id"] = new_id
            entries.append(
                dict(
                    action="activate",
                    version_id=new_id,
                    old_state=GENERATED,
                    new_state=ACTIVE,
                    result="success",
                    detail={"reason": "rotate"},
                )
            )
            self._write_state_locked()
            audit_entries = [
                self._append_audit_locked(request_id=request_id, **kw) for kw in entries
            ]
            return new_id, audit_entries[-1]

    def activate(self, version_id: str, request_id: str | None = None) -> AuditEntry:
        """激活某个 generated 版本；当前 active 版本（若有）同时停用。"""
        with self._thread_lock:
            assert self._state is not None
            rec = self._record(version_id)
            cur = rec["state"]
            if cur == ACTIVE:
                # 幂等：已经是 active，不产生状态变更也不重复记账
                return self._append_audit_locked(
                    action="activate",
                    version_id=version_id,
                    old_state=ACTIVE,
                    new_state=ACTIVE,
                    result="noop",
                    detail={},
                    request_id=request_id,
                )
            if ACTIVE not in _ALLOWED_TRANSITIONS.get(cur, frozenset()):
                raise InvalidStateTransition(f"cannot activate version in state {cur!r}")
            old_active = self._state["active_version_id"]
            audit_kwargs: list[dict[str, Any]] = []
            if old_active is not None and old_active != version_id:
                old_rec = self._record(old_active)
                old_rec["state"] = RETIRED
                audit_kwargs.append(
                    dict(
                        action="deactivate",
                        version_id=old_active,
                        old_state=ACTIVE,
                        new_state=RETIRED,
                        result="success",
                        detail={"reason": "activate_other"},
                    )
                )
            rec["state"] = ACTIVE
            self._state["active_version_id"] = version_id
            audit_kwargs.append(
                dict(
                    action="activate",
                    version_id=version_id,
                    old_state=cur,
                    new_state=ACTIVE,
                    result="success",
                    detail={},
                )
            )
            self._write_state_locked()
            out = [self._append_audit_locked(request_id=request_id, **kw) for kw in audit_kwargs]
            return out[-1]

    def deactivate(self, version_id: str, request_id: str | None = None) -> AuditEntry:
        """停用 active 版本（active -> retired）。"""
        with self._thread_lock:
            assert self._state is not None
            rec = self._record(version_id)
            cur = rec["state"]
            if RETIRED not in _ALLOWED_TRANSITIONS.get(cur, frozenset()):
                raise InvalidStateTransition(f"cannot deactivate version in state {cur!r}")
            rec["state"] = RETIRED
            if self._state["active_version_id"] == version_id:
                self._state["active_version_id"] = None
            return self._commit_locked(
                dict(
                    action="deactivate",
                    version_id=version_id,
                    old_state=ACTIVE,
                    new_state=RETIRED,
                    result="success",
                    detail={},
                    request_id=request_id,
                )
            )

    def destroy(self, version_id: str, request_id: str | None = None) -> AuditEntry:
        """销毁版本：任何非 destroyed 状态都可销毁；删除密钥材料，不可恢复。"""
        with self._thread_lock:
            assert self._state is not None
            rec = self._record(version_id)
            cur = rec["state"]
            if cur == DESTROYED:
                return self._append_audit_locked(
                    action="destroy",
                    version_id=version_id,
                    old_state=DESTROYED,
                    new_state=DESTROYED,
                    result="noop",
                    detail={},
                    request_id=request_id,
                )
            rec["state"] = DESTROYED
            # 不可恢复：删除被包装的密钥材料，仅保留指纹占位（指纹不可逆）
            rec["wrapped_dek"] = None
            if self._state["active_version_id"] == version_id:
                self._state["active_version_id"] = None
            return self._commit_locked(
                dict(
                    action="destroy",
                    version_id=version_id,
                    old_state=cur,
                    new_state=DESTROYED,
                    result="success",
                    detail={"material_purged": True},
                    request_id=request_id,
                )
            )

    # ------------------------------------------------------------------
    # 只读视图 / 密钥取用（service 层在 locked() 内调用）
    # ------------------------------------------------------------------
    def active_version_id(self) -> str | None:
        with self._thread_lock:
            assert self._state is not None
            return self._state["active_version_id"]

    def version_state(self, version_id: str) -> str:
        with self._thread_lock:
            return self._record(version_id)["state"]

    def load_dek(self, version_id: str) -> bytes:
        """取出某版本的 DEK；destroyed 版本材料已清除，抛 DecryptVersionDestroyed。"""
        with self._thread_lock:
            import base64

            rec = self._record(version_id)
            blob = rec.get("wrapped_dek")
            if blob is None:
                raise DecryptVersionDestroyed(
                    f"version {version_id!r} destroyed; key material gone"
                )
            assert self._kek is not None
            return crypto.unwrap_dek(self._kek, base64.b64decode(blob))

    def list_versions(self) -> list[dict[str, Any]]:
        with self._thread_lock:
            assert self._state is not None
            active_id = self._state["active_version_id"]
            out = []
            for vid, rec in sorted(self._state["versions"].items(), key=lambda kv: kv[1]["version"]):
                out.append(
                    {
                        "version_id": vid,
                        "seq": rec["version"],
                        "state": rec["state"],
                        "active": vid == active_id,
                        "created_ts": rec["created_ts"],
                        "fingerprint_sha256": rec["fingerprint"],
                        "has_material": rec.get("wrapped_dek") is not None,
                    }
                )
            return out
