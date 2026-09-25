"""本地测试密钥的加载与生成（JSON keystore）。

keystore 形态::

    {
      "version": 1,
      "keys": {
        "demo-key-1": {"secret_hex": "<64 hex chars>", "created_at": "..."}
      }
    }

* 仅用于本地测试：密钥由 OS CSPRNG 生成（见 :func:`anti_replay.crypto.generate_key`）。
* 加载时强制文件权限为 0600（属主可读写，其他人无任何权限），
  防止测试密钥被同机其他用户读取；权限不合规直接拒绝。
"""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timezone

from .crypto import generate_key

KEYSTORE_VERSION = 1


class KeystoreError(Exception):
    """keystore 文件缺失、损坏或权限不合规。"""


def _check_owner_only(path: str) -> None:
    st = os.stat(path)
    mode = stat.S_IMODE(st.st_mode)
    # 属主可读写；组/其他位必须全部为 0。
    if mode & 0o077:
        raise KeystoreError(
            f"keystore {path} 权限过宽（{mode:04o}）；请先执行 chmod 600 {path}"
        )


def load_keystore(path: str) -> dict[str, bytes]:
    """加载 keystore，返回 ``{key_id: secret_bytes}``。"""
    if not os.path.exists(path):
        raise KeystoreError(f"keystore 不存在：{path}")
    _check_owner_only(path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise KeystoreError(f"无法读取 keystore：{exc}") from exc

    if not isinstance(data, dict) or data.get("version") != KEYSTORE_VERSION:
        raise KeystoreError("keystore 缺少受支持的 version 字段")
    raw_keys = data.get("keys")
    if not isinstance(raw_keys, dict) or not raw_keys:
        raise KeystoreError("keystore 中没有任何密钥")

    keys: dict[str, bytes] = {}
    for key_id, entry in raw_keys.items():
        if not isinstance(entry, dict) or "secret_hex" not in entry:
            raise KeystoreError(f"密钥 {key_id!r} 条目损坏")
        try:
            secret = bytes.fromhex(entry["secret_hex"])
        except ValueError as exc:
            raise KeystoreError(f"密钥 {key_id!r} 不是合法十六进制") from exc
        if len(secret) < 16:
            raise KeystoreError(f"密钥 {key_id!r} 长度不足（至少 16 字节）")
        keys[key_id] = secret
    return keys


def create_or_update_key(
    path: str, key_id: str, *, num_bytes: int = 32, overwrite: bool = False
) -> bytes:
    """生成新密钥并写入（或更新）keystore。

    文件以 0600 权限创建。返回生成的密钥字节。
    """
    if os.path.exists(path):
        _check_owner_only(path)
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        if content.strip() == "":
            # mktemp 之类预置的空文件：当作全新 keystore。
            data = {"version": KEYSTORE_VERSION, "keys": {}}
        else:
            try:
                data = json.loads(content)
            except json.JSONDecodeError as exc:
                raise KeystoreError(f"现有 keystore 不是合法 JSON：{exc}") from exc
        if data.get("version") != KEYSTORE_VERSION or not isinstance(
            data.get("keys"), dict
        ):
            raise KeystoreError("现有 keystore 格式不受支持")
    else:
        data = {"version": KEYSTORE_VERSION, "keys": {}}

    if key_id in data["keys"] and not overwrite:
        raise KeystoreError(f"密钥 {key_id!r} 已存在（使用 --overwrite 覆盖）")

    if not isinstance(num_bytes, int) or num_bytes < 16:
        raise KeystoreError("HMAC-SHA256 密钥长度至少为 16 字节（建议 32）")

    secret = generate_key(num_bytes)
    data["keys"][key_id] = {
        "secret_hex": secret.hex(),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    # 先以 0600 建文件，再写内容，避免短暂的宽权限窗口。
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
    except Exception:
        # fdopen 成功后异常由 with 负责关闭；仅 fdopen 本身失败时需手动关闭。
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    os.chmod(path, 0o600)
    return secret
