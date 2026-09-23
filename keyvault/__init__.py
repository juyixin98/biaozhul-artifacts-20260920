"""keyvault — 本地密钥版本管理与审计服务。

纯后端、本地运行。密码原语全部来自 cryptography 库（AES-256-GCM），
测试密钥由本机 CSPRNG 生成，不接任何生产账号，不含自研加密算法。
"""

from .errors import (
    DecryptError,
    InvalidStateTransitionError,
    KeyDestroyedError,
    KeyNotFoundError,
    KeyVaultError,
    NoActiveKeyError,
    WrongVersionError,
)
from .models import KeyState
from .service import KeyService

__all__ = [
    "KeyService",
    "KeyState",
    "KeyVaultError",
    "KeyNotFoundError",
    "InvalidStateTransitionError",
    "KeyDestroyedError",
    "NoActiveKeyError",
    "DecryptError",
    "WrongVersionError",
]

__version__ = "0.1.0"
