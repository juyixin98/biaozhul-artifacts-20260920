"""密码原语封装。

设计约束（对应需求）：

* 不自创加密算法：加密使用 Fernet（AES-128-CBC + HMAC-SHA256 认证加密），
  密钥推导使用 HKDF-SHA256，哈希使用 SHA-256，均来自 ``cryptography``；
* 不接生产账号：主密钥只在本地生成（``os.urandom``），以 0600 权限落盘，
  路径由环境变量 / 参数指定，默认在项目 ``.secrets/`` 下（仅测试用）；
* 哈希为确定性 SHA-256，输出十六进制；不把盐或原文写进异常消息。
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .errors import TransformError

DEFAULT_KEY_PATH = Path(os.environ.get("DMS_KEY_FILE", ".secrets/dms-test-key.json"))
_FERNET_INFO = b"dms/v1/fernet"
_HASH_SALT_INFO = b"dms/v1/hash-salt"
_MASTER_LEN = 32


class CryptoProvider:
    """持有主密钥并提供 encrypt/decrypt/hash。

    同一主密钥经 HKDF 派生出：
      * Fernet 用 32 字节密钥（base64url 编码后喂给 Fernet）；
      * 可选的哈希盐（本版本固定使用空盐，盐值也不写回日志）。
    """

    def __init__(self, master_key: bytes) -> None:
        if len(master_key) != _MASTER_LEN:
            raise TransformError("主密钥长度必须为 32 字节")
        self._master = master_key
        fernet_key = HKDF(
            algorithm=hashes.SHA256(), length=32, salt=None, info=_FERNET_INFO
        ).derive(master_key)
        self._fernet = Fernet(base64.urlsafe_b64encode(fernet_key))

    @classmethod
    def generate(cls) -> "CryptoProvider":
        return cls(os.urandom(_MASTER_LEN))

    @classmethod
    def load_or_create(cls, path: Path | str = DEFAULT_KEY_PATH) -> "CryptoProvider":
        path = Path(path)
        if path.exists():
            return cls.load(path)
        provider = cls.generate()
        provider.save(path)
        return provider

    @classmethod
    def load(cls, path: Path | str) -> "CryptoProvider":
        path = Path(path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise TransformError("无法读取密钥文件")
        try:
            master = base64.b64decode(data["master_key_b64"])
        except (KeyError, TypeError, ValueError):
            raise TransformError("密钥文件格式无效")
        if data.get("version") != 1:
            raise TransformError("不支持的密钥文件版本")
        return cls(master)

    def save(self, path: Path | str) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"version": 1, "master_key_b64": base64.b64encode(self._master).decode()}
        )
        path.write_text(payload, encoding="utf-8")
        os.chmod(path, 0o600)
        return path

    def encrypt(self, plaintext: str) -> str:
        if not isinstance(plaintext, str):
            raise TransformError("encrypt 仅接受字符串标量", details={"rule_hint": "encrypt"})
        token = self._fernet.encrypt(plaintext.encode("utf-8"))
        return token.decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        if not isinstance(ciphertext, str):
            raise TransformError("decrypt 仅接受字符串标量", details={"rule_hint": "decrypt"})
        try:
            return self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError):
            # 绝不回显输入值
            raise TransformError("decrypt 失败：密文无效或密钥不匹配",
                                 details={"rule_hint": "decrypt"})

    @staticmethod
    def hash_value(value: str) -> str:
        if not isinstance(value, str):
            raise TransformError("hash 仅接受字符串标量", details={"rule_hint": "hash"})
        digest = hashes.Hash(hashes.SHA256())
        digest.update(value.encode("utf-8"))
        return digest.finalize().hex()
