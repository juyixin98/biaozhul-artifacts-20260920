"""信封加密轮换（envelope encryption with master-key rotation）本地服务。

纯后端、仅用于本地数据处理与测试：
- 密码原语全部来自 cryptography（AES-256-GCM），不自创算法；
- 主密钥/数据密钥均为本地随机生成，不接入任何生产账号或 KMS。

公开 API：
    crypto      —— AEAD、nonce、AAD 等底层原语
    keyring     —— 本地主密钥环（JSON 文件，0600 权限，仅供测试/开发）
    container   —— 分块 AEAD 容器：加密 / 解密 / 主密钥轮换
    cli/server  —— 命令行与仅监听 127.0.0.1 的演示 HTTP 接口
"""

from .crypto import (
    KEY_LEN,
    NONCE_LEN,
    TAG_LEN,
    InvalidTag,
    chunk_nonce,
)
from .keyring import Keyring, MasterKey
from .container import (
    DEFAULT_CHUNK_SIZE,
    ContainerError,
    IntegrityError,
    KeyNotFoundError,
    TruncatedContainer,
    decrypt_bytes,
    decrypt_file,
    encrypt_bytes,
    encrypt_file,
    parse_header,
    rotate_bytes,
    rotate_file,
    rotate_many,
)

__version__ = "1.0.0"

__all__ = [
    "__version__",
    # crypto
    "KEY_LEN",
    "NONCE_LEN",
    "TAG_LEN",
    "InvalidTag",
    "chunk_nonce",
    # keyring
    "Keyring",
    "MasterKey",
    # container
    "DEFAULT_CHUNK_SIZE",
    "ContainerError",
    "IntegrityError",
    "KeyNotFoundError",
    "TruncatedContainer",
    "decrypt_bytes",
    "decrypt_file",
    "encrypt_bytes",
    "encrypt_file",
    "parse_header",
    "rotate_bytes",
    "rotate_file",
    "rotate_many",
]
