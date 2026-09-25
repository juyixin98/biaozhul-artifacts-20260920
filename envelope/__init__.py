"""信封加密 / 主密钥轮换 —— 纯后端本地安全数据处理服务。

公开接口：

- :class:`envelope.service.EnvelopeService`：核心业务（分块 AEAD 加解密、密钥轮换）
- :class:`envelope.keystore.KeyStore`：本地主密钥与包裹 nonce 计数器
- :func:`envelope.crypto.generate_key`：测试用本地密钥生成（OS RNG）

本包只使用 ``cryptography`` 提供的标准密码原语（AES-256-GCM），
不包含任何自创密码算法，也不接入任何生产账号 / KMS。
"""

from .errors import (
    AEADAuthenticationError,
    CorruptContainerError,
    EnvelopeError,
    InvalidFormatError,
    KeyNotFoundError,
    NoMasterKeyError,
    RotationError,
    TruncatedContainerError,
)
from .keystore import KeyStore, StoredMasterKey
from .service import EncryptedLocation, EnvelopeService, RotationInfo

__all__ = [
    "EnvelopeService",
    "EncryptedLocation",
    "RotationInfo",
    "KeyStore",
    "StoredMasterKey",
    "EnvelopeError",
    "AEADAuthenticationError",
    "CorruptContainerError",
    "TruncatedContainerError",
    "InvalidFormatError",
    "KeyNotFoundError",
    "NoMasterKeyError",
    "RotationError",
]
