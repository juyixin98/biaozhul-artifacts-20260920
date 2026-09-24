"""本地信任状态的持久化与原子提交。

状态文件 ``state.json`` 始终通过 *同目录临时文件 + fsync + os.replace* 提交，
任何中途崩溃都不会留下半截状态；目标文件以内容哈希寻址存放在 ``blobs/`` 中，
先落盘再更新状态，提交后清理不再被引用的孤儿 blob。

跨进程串行化使用 ``flock``；同一进程内由 API 层的 asyncio.Lock 保证。
"""

from __future__ import annotations

import base64
import fcntl
import json
import os
import tempfile
from pathlib import Path

STATE_FILENAME = "state.json"
LOCK_FILENAME = "state.lock"
BLOBS_DIRNAME = "blobs"

_INITIAL_STATE = {
    "root": None,
    "timestamp": None,
    "snapshot": None,
    "targets": None,
    # sha256 -> {"file": "blobs/xxx", "sha256": ..., "sha512": ..., "length": n}
    "blobs": {},
    # 目标名 -> {"sha256": ..., "sha512": ..., "length": n}
    "target_files": {},
}


def b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64d(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"), validate=True)


class TrustStore:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.blobs_dir = self.data_dir / BLOBS_DIRNAME
        self.state_path = self.data_dir / STATE_FILENAME
        self.lock_path = self.data_dir / LOCK_FILENAME

    # ---- 生命周期 -------------------------------------------------------

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.blobs_dir.mkdir(parents=True, exist_ok=True)
        if not self.state_path.exists():
            self._write_state(_INITIAL_STATE)

    def reset(self) -> None:
        """清空全部信任状态（仅供管理接口 / 测试使用）。"""
        if self.state_path.exists():
            self.state_path.unlink()
        if self.lock_path.exists():
            self.lock_path.unlink()
        for child in self.blobs_dir.glob("*"):
            child.unlink()
        self.ensure_dirs()

    def lock(self):
        """跨进程互斥（配合上下文管理器使用）。"""
        self.ensure_dirs()
        self._fh = open(self.lock_path, "a+")
        fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        return self

    def unlock(self) -> None:
        fh = getattr(self, "_fh", None)
        if fh is not None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            fh.close()
            self._fh = None

    def __enter__(self):
        return self.lock()

    def __exit__(self, *exc):
        self.unlock()

    # ---- 状态读写 -------------------------------------------------------

    def load(self) -> dict:
        self.ensure_dirs()
        with open(self.state_path, "r", encoding="utf-8") as fh:
            state = json.load(fh)
        # 补齐未来可能新增的字段
        for key, value in _INITIAL_STATE.items():
            state.setdefault(key, value if value is None else json.loads(json.dumps(value)))
        return state

    def commit(self, state: dict, *, drop_blob_sha256: set[str] | None = None) -> None:
        """原子提交新状态；提交成功后删除不再被引用的 blob。"""
        self._write_state(state)
        referenced = set(state.get("blobs", {}).keys())
        for blob in self.blobs_dir.glob("*"):
            if blob.name not in referenced:
                try:
                    blob.unlink()
                except FileNotFoundError:
                    pass

    def _write_state(self, state: dict) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".state.", suffix=".tmp", dir=self.data_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh, sort_keys=True, indent=2)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self.state_path)
            self._fsync_dir(self.data_dir)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
            raise

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    # ---- 内容寻址 blob --------------------------------------------------

    def blob_path(self, sha256: str) -> Path:
        return self.blobs_dir / sha256

    def has_blob(self, sha256: str) -> bool:
        return sha256 in self._loaded_blobs()

    def _loaded_blobs(self) -> dict:
        return self.load()["blobs"]

    def put_blob(self, data: bytes, *, sha256: str, sha512: str, length: int) -> str:
        """把目标文件写入 blob 存储（内容寻址，已存在则直接复用）。返回相对路径。"""
        path = self.blob_path(sha256)
        if not path.exists():
            self.blobs_dir.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(prefix=".blob.", suffix=".tmp", dir=self.blobs_dir)
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp_name, path)
                self._fsync_dir(self.blobs_dir)
            except BaseException:
                try:
                    os.unlink(tmp_name)
                except FileNotFoundError:
                    pass
                raise
        return f"{BLOBS_DIRNAME}/{sha256}"

    def read_blob(self, sha256: str) -> bytes | None:
        path = self.blob_path(sha256)
        if not path.exists():
            return None
        return path.read_bytes()
