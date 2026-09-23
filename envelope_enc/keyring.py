"""本地主密钥环（仅用于本地处理/测试，禁止用于生产）。

存储形态：一个 JSON 文件，0600 权限（仅属主可读写），形如：

    {
      "keys": {
        "<kid>": {"key_b64": "...", "created_at": "2026-09-24T...Z",
                  "status": "active|retired"}
      },
      "active_kid": "<kid>"
    }

设计取舍：
- 真正生产环境里主密钥应放在 HSM/KMS 中，本项目明确"不接生产账号"，
  因此主密钥只是本地随机字节，落盘文件本身没有额外的静态加密
  （本机文件系统权限是唯一屏障）。README 中会重复这一警告。
- kid 形如 ``mk-0001-<10位随机>``，随机部分避免轮换时发生 kid 碰撞，
  数字前缀方便人眼区分新旧。
"""

from __future__ import annotations

import base64
import json
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

from .crypto import KEY_LEN, random_key

#: 仅属主读写
_FILE_MODE = 0o600


class KeyringError(Exception):
    """密钥环错误（文件损坏、kid 不存在等）。"""


@dataclass(frozen=True)
class MasterKey:
    kid: str
    key: bytes
    created_at: str
    status: str = "active"

    @property
    def active(self) -> bool:
        return self.status == "active"


class Keyring:
    """JSON 文件支持的本地主密钥环。

    典型用法::

        kr = Keyring("keys.json")
        kr.initialize()          # 首次：生成第一把 active 主密钥
        mk = kr.active()         # 取当前主密钥
        kr.rotate_master()       # 生成新主密钥，旧的转为 retired（仍可解密）
    """

    def __init__(self, path: str):
        self.path = path

    # ------------------------------------------------------------------ 持久化
    def _load(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError as exc:
            raise KeyringError(f"密钥环不存在：{self.path}（请先 initialize）") from exc
        except json.JSONDecodeError as exc:
            raise KeyringError(f"密钥环文件损坏（非法 JSON）：{self.path}") from exc
        if not isinstance(data, dict) or "keys" not in data or "active_kid" not in data:
            raise KeyringError(f"密钥环文件结构非法：{self.path}")
        return data

    def _save(self, data: dict) -> None:
        """原子写入：同目录临时文件 + fsync + os.replace，避免写一半损坏。"""
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        tmp_path = f"{self.path}.tmp-{os.getpid()}-{secrets.token_hex(4)}"
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _FILE_MODE)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.path)
            _fsync_dir(directory)
            # replace 之后按创建掩码权限可能不是 0600，显式收紧
            os.chmod(self.path, _FILE_MODE)
        except BaseException:
            _unlink_quiet(tmp_path)
            raise

    # ------------------------------------------------------------------ 初始化
    def exists(self) -> bool:
        return os.path.exists(self.path)

    def initialize(self, *, exist_ok: bool = False) -> MasterKey:
        """若密钥环不存在则创建，并生成第一把 active 主密钥。"""
        if self.exists():
            if exist_ok:
                return self.active()
            raise KeyringError(f"密钥环已存在：{self.path}")
        mk = _new_master_key(1)
        kid, entry = _entry(mk)
        data = {"keys": {kid: entry}, "active_kid": mk.kid}
        self._save(data)
        return mk

    # ------------------------------------------------------------------ 查询
    def _get(self, kid: str) -> MasterKey:
        data = self._load()
        entry = data["keys"].get(kid)
        if entry is None:
            raise KeyringError(f"主密钥不存在：{kid}")
        return _decode_entry(kid, entry)

    def active(self) -> MasterKey:
        data = self._load()
        return self._get(data["active_kid"])

    def get_for_unwrap(self, kid: str) -> MasterKey:
        """按 kid 取主密钥用于解密：active 与 retired 都允许。"""
        return self._get(kid)

    def list_keys(self) -> list[MasterKey]:
        data = self._load()
        return [
            _decode_entry(kid, entry)
            for kid, entry in sorted(data["keys"].items())
        ]

    # ------------------------------------------------------------------ 轮换
    def rotate_master(self) -> MasterKey:
        """生成新的 active 主密钥；当前 active 转为 retired。

        retired 密钥保留在环里——它仍用于解密/轮换旧文件，只是不再加密新数据。
        确认所有文件都已迁移后，可用 :meth:`delete_key` 显式删除。
        """
        data = self._load()
        old_kid = data["active_kid"]
        old = data["keys"].get(old_kid)
        if old is None:
            raise KeyringError(f"当前 active_kid 在环中不存在：{old_kid}")
        old["status"] = "retired"

        seq = len(data["keys"]) + 1
        mk = _new_master_key(seq)
        data["keys"][mk.kid] = {
            "key_b64": base64.b64encode(mk.key).decode("ascii"),
            "created_at": mk.created_at,
            "status": mk.status,
        }
        data["active_kid"] = mk.kid
        self._save(data)
        return mk

    def delete_key(self, kid: str) -> None:
        """彻底删除一把主密钥（通常在所有文件轮换完成后删除 retired 旧密钥）。

        禁止删除当前 active 密钥。删除后，仍由它包裹的文件将永远无法解密。
        """
        data = self._load()
        if kid == data["active_kid"]:
            raise KeyringError("不能删除当前 active 主密钥，请先轮换")
        if kid not in data["keys"]:
            raise KeyringError(f"主密钥不存在：{kid}")
        del data["keys"][kid]
        self._save(data)


# ---------------------------------------------------------------------- 工具函数
def _new_master_key(seq: int) -> MasterKey:
    return MasterKey(
        kid=f"mk-{seq:04d}-{secrets.token_hex(5)}",
        key=random_key(),
        created_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        status="active",
    )


def _entry(mk: MasterKey) -> tuple[str, dict]:
    return (
        mk.kid,
        {
            "key_b64": base64.b64encode(mk.key).decode("ascii"),
            "created_at": mk.created_at,
            "status": mk.status,
        },
    )


def _decode_entry(kid: str, entry: dict) -> MasterKey:
    try:
        key = base64.b64decode(entry["key_b64"], validate=True)
    except Exception as exc:  # noqa: BLE001 - 损坏的 base64 统一归类
        raise KeyringError(f"主密钥 {kid} 的 key_b64 非法") from exc
    if len(key) != KEY_LEN:
        raise KeyringError(f"主密钥 {kid} 长度不是 {KEY_LEN} 字节")
    return MasterKey(
        kid=kid,
        key=key,
        created_at=entry.get("created_at", ""),
        status=entry.get("status", "active"),
    )


def _fsync_dir(directory: str) -> None:
    """fsync 目录，保证 rename 在断电后也已落盘（不支持时静默跳过）。"""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _unlink_quiet(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
