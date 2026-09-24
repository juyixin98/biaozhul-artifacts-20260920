"""主密钥库与对象存储 (内存实现; 接口可替换为持久化实现)。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from threading import RLock

from . import crypto
from .crypto import (
    DEFAULT_BLOCK_SIZE,
    EnvelopeError,
    KeyUnavailableError,
    new_key,
    open_container,
    parse_header,
    rewrap_header,
    seal,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RotationInterrupted(EnvelopeError):
    """模拟轮换中途崩溃: 新主密钥已建立, 仅部分对象完成重包裹。"""

    def __init__(self, new_key_id: str, rewrapped: int, remaining: int):
        super().__init__(f"轮换中断: 新密钥 {new_key_id}, 已重包裹 {rewrapped}, 剩余 {remaining}")
        self.new_key_id = new_key_id
        self.rewrapped = rewrapped
        self.remaining = remaining


@dataclass(frozen=True)
class MasterKeyInfo:
    key_id: str
    created_at: str
    is_current: bool


class KeyStore:
    """保存所有版本的主密钥; 轮换即新增版本, 旧版本保留以供解密旧对象。"""

    def __init__(self) -> None:
        self._lock = RLock()
        self._materials: dict[str, bytes] = {}
        self._created_at: dict[str, str] = {}
        self._order: list[str] = []
        self.rotate()  # 初始主密钥 v1

    @property
    def current_id(self) -> str:
        return self._order[-1]

    def rotate(self) -> str:
        """生成新版本主密钥, 返回其 key_id。旧版本不删除。"""
        with self._lock:
            key_id = f"v{len(self._order) + 1}"
            self._materials[key_id] = new_key()
            self._created_at[key_id] = _now()
            self._order.append(key_id)
            return key_id

    def resolve(self, key_id: str) -> bytes:
        try:
            return self._materials[key_id]
        except KeyError as exc:
            raise KeyUnavailableError(f"主密钥 {key_id} 不可用 (已删除 / 未知版本)") from exc

    def discard(self, key_id: str) -> None:
        """模拟旧密钥被安全删除; 不允许删除当前主密钥。"""
        with self._lock:
            if key_id not in self._materials:
                raise KeyUnavailableError(f"主密钥 {key_id} 不存在")
            if key_id == self.current_id:
                raise ValueError("不能删除当前主密钥")
            self._materials.pop(key_id)
            self._order.remove(key_id)

    def list_keys(self) -> list[MasterKeyInfo]:
        with self._lock:
            current = self.current_id
            return [
                MasterKeyInfo(kid, self._created_at[kid], kid == current)
                for kid in self._order
            ]


@dataclass
class StoredObject:
    name: str
    container: bytes
    created_at: str
    updated_at: str


class ObjectStore:
    """内存对象存储: 保存的是信封容器 (加密后字节), 从不保存明文。"""

    def __init__(self, keys: KeyStore) -> None:
        self._keys = keys
        self._lock = RLock()
        self._objects: dict[str, StoredObject] = {}

    def put(self, name: str, plaintext: bytes, block_size: int = DEFAULT_BLOCK_SIZE) -> StoredObject:
        container = seal(self._keys.resolve(self._keys.current_id), self._keys.current_id,
                         plaintext, block_size)
        ts = _now()
        with self._lock:
            existing = self._objects.get(name)
            obj = StoredObject(
                name=name,
                container=container,
                created_at=existing.created_at if existing else ts,
                updated_at=ts,
            )
            self._objects[name] = obj
            return obj

    def _get_required(self, name: str) -> StoredObject:
        try:
            return self._objects[name]
        except KeyError as exc:
            raise KeyError(f"对象 {name!r} 不存在") from exc

    def open(self, name: str) -> bytes:
        obj = self._get_required(name)
        with self._lock:
            container = obj.container
        return open_container(container, self._keys.resolve)

    def get_container(self, name: str) -> bytes:
        return self._get_required(name).container

    def set_container(self, name: str, container: bytes) -> None:
        """测试 / 故障注入辅助: 直接替换容器字节 (用于篡改 / 截断演示)。"""
        with self._lock:
            obj = self._get_required(name)
            obj.container = container
            obj.updated_at = _now()

    def metadata(self, name: str) -> dict:
        obj = self._get_required(name)
        header, _ = parse_header(obj.container)
        return {
            "name": name,
            "version": header.version,
            "key_id": header.key_id,
            "block_size": header.block_size,
            "plaintext_len": header.plaintext_len,
            "salt_hex": header.salt.hex(),
            "created_at": obj.created_at,
            "updated_at": obj.updated_at,
        }

    def list_objects(self) -> list[str]:
        return sorted(self._objects)

    def rotate_master(self, fail_after: int | None = None) -> dict:
        """轮换主密钥并重包裹所有对象的 DEK。

        fail_after: 若给定 (>=0), 重包裹完该数量对象后模拟崩溃 ——
        新密钥已建立且持久化, 但部分对象仍由旧密钥包裹 (混合状态)。
        再次调用 (fail_after=None) 可完成剩余对象。
        """
        new_key_id = self._keys.rotate()
        return self.rewrap_all(fail_after=fail_after, new_key_id=new_key_id)

    def rewrap_all(self, fail_after: int | None = None, new_key_id: str | None = None) -> dict:
        """把所有非当前密钥包裹的对象重包裹到 (指定或当前) 主密钥; 数据块不动。"""
        target_id = new_key_id or self._keys.current_id
        target_key = self._keys.resolve(target_id)
        with self._lock:
            pending = [
                name
                for name in sorted(self._objects)
                if parse_header(self._objects[name].container)[0].key_id != target_id
            ]
            rewrapped = 0
            for name in pending:
                if fail_after is not None and rewrapped >= fail_after:
                    raise RotationInterrupted(
                        new_key_id=target_id,
                        rewrapped=rewrapped,
                        remaining=len(pending) - rewrapped,
                    )
                obj = self._objects[name]
                obj.container = rewrap_header(
                    obj.container, self._keys.resolve, target_key, target_id
                )
                obj.updated_at = _now()
                rewrapped += 1
        return {"new_key_id": target_id, "rewrapped": rewrapped, "total_pending": len(pending)}
