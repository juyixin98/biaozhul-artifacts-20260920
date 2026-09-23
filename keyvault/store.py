"""持久化存储层。

设计目标：崩溃后状态一致。
  * 元数据 state.json 与密钥材料 keys/<id>.key 分离存放；
  * 所有写入走"临时文件 + fsync + os.replace + 目录 fsync"的原子替换，
    任意时刻崩溃，磁盘上要么是旧版本、要么是新版本，不会出现半写文件；
  * 密钥文件权限 0600；
  * 销毁 = 用随机数据覆写密钥文件并 fsync 后再删除，随后原子更新元数据。
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
from pathlib import Path
from typing import Dict, Optional

from .models import KeyMetadata, KeyState


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    """原子写入：临时文件 + fsync + os.replace + 目录 fsync。"""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class KeyStore:
    """密钥元数据与密钥材料的本地持久化存储。"""

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.keys_dir = self.data_dir / "keys"
        self.state_path = self.data_dir / "state.json"
        self.keys_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.keys_dir, 0o700)
        if not self.state_path.exists():
            self._save({"next_version": 1, "active_version": None, "versions": {}})

    # ---------- 元数据 ----------

    def _load(self) -> dict:
        with open(self.state_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _save(self, state: dict) -> None:
        blob = json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8")
        _atomic_write(self.state_path, blob)

    def allocate_version_id(self) -> str:
        state = self._load()
        vid = f"v{state['next_version']}"
        state["next_version"] += 1
        self._save(state)
        return vid

    def put_metadata(self, meta: KeyMetadata) -> None:
        state = self._load()
        state["versions"][meta.version_id] = meta.to_dict()
        self._save(state)

    def get_metadata(self, version_id: str) -> Optional[KeyMetadata]:
        state = self._load()
        d = state["versions"].get(version_id)
        return KeyMetadata.from_dict(d) if d else None

    def list_metadata(self) -> Dict[str, KeyMetadata]:
        state = self._load()
        return {vid: KeyMetadata.from_dict(d) for vid, d in state["versions"].items()}

    def get_active_version(self) -> Optional[str]:
        return self._load()["active_version"]

    def set_active_version(self, version_id: Optional[str]) -> None:
        state = self._load()
        state["active_version"] = version_id
        self._save(state)

    # ---------- 密钥材料 ----------

    def _key_path(self, version_id: str) -> Path:
        # version_id 由本服务生成（v<整数>），仍做一次校验防路径穿越
        if not version_id.startswith("v") or not version_id[1:].isdigit():
            raise ValueError(f"非法版本号: {version_id!r}")
        return self.keys_dir / f"{version_id}.key"

    def save_key_material(self, version_id: str, key: bytes) -> None:
        _atomic_write(self._key_path(version_id), key, mode=0o600)

    def load_key_material(self, version_id: str) -> Optional[bytes]:
        p = self._key_path(version_id)
        if not p.exists():
            return None
        return p.read_bytes()

    def destroy_key_material(self, version_id: str) -> None:
        """安全擦除：随机覆写 + fsync + 删除 + 目录 fsync。"""
        p = self._key_path(version_id)
        if p.exists():
            size = p.stat().st_size
            with open(p, "r+b") as f:
                f.write(secrets.token_bytes(size))
                f.flush()
                os.fsync(f.fileno())
            p.unlink()
            _fsync_dir(self.keys_dir)

    # ---------- 崩溃一致性自检 ----------

    def check_consistency(self) -> list[str]:
        """启动时自检：返回不一致项列表（空列表 = 一致）。"""
        problems = []
        metas = self.list_metadata()
        active = self.get_active_version()

        if active is not None:
            m = metas.get(active)
            if m is None:
                problems.append(f"active_version={active} 无对应元数据")
            elif m.state != KeyState.ACTIVE:
                problems.append(f"active_version={active} 但状态为 {m.state.value}")

        for vid, m in metas.items():
            has_material = self._key_path(vid).exists()
            if m.state == KeyState.DESTROYED and has_material:
                problems.append(f"{vid} 已销毁但密钥材料仍存在")
            if m.state != KeyState.DESTROYED and not has_material:
                problems.append(f"{vid} 未销毁但密钥材料缺失")
        return problems
