"""本地主密钥库。

存储布局（JSON，文件权限 0600，目录权限 0700）::

    {
      "version": 1,
      "next_wrap_counter": 17,
      "next_key_seq": 3,
      "keys": {
        "mk_ab12...": {
          "kid": "mk_ab12...",
          "key_b64": "<base64 的 32 字节主密钥>",
          "created_at": "2026-09-24T15:30:00Z",
          "seq": 2
        }
      }
    }

nonce 唯一性
------------
``next_wrap_counter`` 是主密钥包裹 nonce 的**全局持久化单调计数器**。
:meth:`KeyStore.allocate_wrap_counter` 先把递增后的值原子落盘，再返回该值，
因此即使随后进程崩溃，该 nonce 也绝不会被第二次分配（计数器空洞不影响安全性，
只意味着跳过了若干未使用的 nonce）。计数器的持久化通过
``写入临时文件 + fsync + 原子 rename + 目录 fsync`` 完成。

注意：这是本地测试用实现。生产环境应使用 HSM / KMS，或至少使用
硬件保护与最小权限的密钥存储；本项目不接入任何生产账号。
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .crypto import KEY_BYTES, generate_key
from .errors import KeyNotFoundError

KEYSTORE_VERSION = 1
_KEY_PREFIX = "mk_"
_FILE_NAME = "keystore.json"
_DIR_MODE = 0o700
_FILE_MODE = 0o600


def _utc_now_iso() -> str:
    """返回秒级 UTC 时间戳（``...Z``）。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"), validate=True)


@dataclass(frozen=True)
class StoredMasterKey:
    """主密钥条目。"""

    kid: str
    key_material: bytes
    created_at: str


class KeyStore:
    """基于本地 JSON 文件的主密钥库。

    所有变更都经过同一把进程内互斥锁串行化，并以原子 rename 落盘；
    多进程场景下方法内部还会对锁文件加 ``flock`` 排他锁。
    """

    def __init__(self, keys_dir: str | os.PathLike[str]):
        self._dir = Path(keys_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self._dir, _DIR_MODE)
        self._path = self._dir / _FILE_NAME
        self._lock_path = self._dir / ".keystore.lock"
        import threading

        self._lock = threading.Lock()
        if not self._path.exists():
            self._write_state(self._empty_state())
        else:
            # 已存在则校验可读（结构问题尽早暴露），并确保文件权限收紧
            self._read_state()
            os.chmod(self._path, _FILE_MODE)

    @property
    def path(self) -> Path:
        return self._path

    @staticmethod
    def _empty_state() -> dict:
        return {
            "version": KEYSTORE_VERSION,
            "next_wrap_counter": 1,
            "next_key_seq": 1,
            "keys": {},
        }

    # ------------------------------------------------------------------ 读写

    def _read_state(self) -> dict:
        with open(self._path, "rb") as fh:
            raw = fh.read()
        if not raw:
            raise ValueError(f"密钥库文件为空：{self._path}")
        state = json.loads(raw.decode("utf-8"))
        if state.get("version") != KEYSTORE_VERSION:
            raise ValueError(f"不支持的密钥库版本：{state.get('version')!r}")
        if not isinstance(state.get("keys"), dict):
            raise ValueError("密钥库结构损坏：缺少 keys")
        if not isinstance(state.get("next_wrap_counter"), int):
            raise ValueError("密钥库结构损坏：缺少 next_wrap_counter")
        if "next_key_seq" not in state:
            # 兼容最初没有该字段的密钥库：按当前条目数回填
            state["next_key_seq"] = len(state["keys"]) + 1
        elif not isinstance(state["next_key_seq"], int):
            raise ValueError("密钥库结构损坏：next_key_seq 非法")
        return state

    def _write_state(self, state: dict) -> None:
        """0600 权限原子写入：临时文件 + fsync + rename + 目录 fsync。"""
        fd, tmp_name = tempfile.mkstemp(prefix=".keystore.", suffix=".tmp", dir=self._dir)
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(json.dumps(state, indent=2, sort_keys=True).encode("utf-8"))
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, _FILE_MODE)
            os.replace(tmp, self._path)
            _fsync_dir(self._dir)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    class _FileLock:
        """flock 排他锁（同一台机器上的多进程互斥）。"""

        def __init__(self, path: Path):
            self._path = path
            self._fh = None

        def __enter__(self):
            import fcntl

            self._fh = open(self._path, "a+b")
            try:
                os.chmod(self._path, 0o600)
            except OSError:
                pass
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
            return self

        def __exit__(self, *exc):
            import fcntl

            assert self._fh is not None
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None
            return False

    # ------------------------------------------------------------------ 接口

    def generate_master_key(self) -> StoredMasterKey:
        """生成并持久化一把新的随机主密钥，返回其条目。

        每把密钥带一个单调递增的 ``seq``（与创建顺序一致，不依赖时间戳精度），
        "最新主密钥"即 seq 最大者。
        """
        with self._lock, self._FileLock(self._lock_path):
            state = self._read_state()
            kid = _KEY_PREFIX + os.urandom(12).hex()
            key_material = generate_key()
            seq = int(state["next_key_seq"])
            entry = StoredMasterKey(kid, key_material, _utc_now_iso())
            state["keys"][kid] = {
                "kid": kid,
                "key_b64": _b64e(key_material),
                "created_at": entry.created_at,
                "seq": seq,
            }
            state["next_key_seq"] = seq + 1
            self._write_state(state)
            return entry

    def get(self, kid: str) -> StoredMasterKey:
        """按 ID 取主密钥；不存在抛 :class:`KeyNotFoundError`。"""
        with self._lock:
            state = self._read_state()
            raw = state["keys"].get(kid)
        if raw is None:
            raise KeyNotFoundError(f"主密钥不存在：{kid}")
        key_material = _b64d(raw["key_b64"])
        if len(key_material) != KEY_BYTES:
            raise ValueError(f"主密钥 {kid} 长度非法")
        return StoredMasterKey(kid, key_material, raw["created_at"])

    def latest_kid(self) -> str:
        """返回最新主密钥 ID（seq 最大，即最后创建的那把）。"""
        with self._lock:
            state = self._read_state()
            entries = list(state["keys"].values())
        if not entries:
            raise KeyNotFoundError("密钥库为空")
        entries.sort(key=lambda e: e.get("seq", 0))
        return entries[-1]["kid"]

    def list_keys(self) -> list[StoredMasterKey]:
        """列出全部主密钥（按创建顺序，新创建的排在最后）。"""
        with self._lock:
            state = self._read_state()
            entries = list(state["keys"].values())
        entries.sort(key=lambda e: e.get("seq", 0))
        return [
            StoredMasterKey(e["kid"], _b64d(e["key_b64"]), e["created_at"])
            for e in entries
        ]

    def allocate_wrap_counter(self) -> int:
        """原子分配一个新的包裹 nonce 计数器值（先落盘后返回）。"""
        with self._lock, self._FileLock(self._lock_path):
            state = self._read_state()
            value = int(state["next_wrap_counter"])
            if value > 0xFFFFFFFFFFFFFFFF:
                raise OverflowError("包裹 nonce 计数器已耗尽，需要轮换密钥库")
            state["next_wrap_counter"] = value + 1
            self._write_state(state)
            return value


def _fsync_dir(directory: Path) -> None:
    """fsync 目录，确保 rename 等元数据变更落盘。"""
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
